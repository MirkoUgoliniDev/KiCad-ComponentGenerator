"""@brief Review 2026-11-16, fixer C1: start-up, crash reports, app paths, build and test tools.

Each finding (R12-*) gets a positive and a negative case that the fix must
tell apart for a structural reason: the encoding a file declares, whether a
read failed or read wrong bytes, whether a process is gone, whether a server
announced the bytes it sent.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from kicadgen import app_paths, crash_report

ROOT = Path(__file__).resolve().parents[1]


def _load(relative, name):
    """Import a file of the repository by path, without touching sys.path."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher():
    """main.py loaded as a module."""
    return _load("main.py", "kicadgen_main_c1")


@pytest.fixture
def native_clean():
    """Leave no native log and no crash hook behind, whatever the test does."""
    yield
    crash_report.discard_native_log()
    crash_report.uninstall()


# -- R12-1: a .env that is not UTF-8 never stops the start-up -----------------

@pytest.mark.parametrize("codec", ["utf-16", "utf-8-sig", "utf-32"])
def test_r12_1_a_env_file_with_a_byte_order_mark_is_decoded(tmp_path, codec):
    """@brief Positive: the mark says what the file is (PowerShell 5.1 `>` writes UTF-16 LE with one)."""
    path = tmp_path / ".env"
    path.write_bytes("KICADGEN_C1=città\n".encode(codec))
    assert app_paths.read_env_text(path) == "KICADGEN_C1=città\n"
    assert app_paths.unreadable_env_files([path]) == []


@pytest.mark.parametrize("raw", [
    "KICADGEN_C1=città\n".encode("cp1252"),       # ANSI, no mark: invalid UTF-8
    "KICADGEN_C1=x\n".encode("utf-16-le"),         # UTF-16 without a mark: NULs
], ids=["ansi", "utf16-no-mark"])
def test_r12_1_a_file_without_a_mark_that_is_not_utf8_is_refused_not_guessed(tmp_path, raw):
    """@brief Negative: no code page is guessed; the file is named as an encoding problem."""
    path = tmp_path / ".env"
    path.write_bytes(raw)
    with pytest.raises(app_paths.EnvFileEncodingError):
        app_paths.read_env_text(path)
    assert isinstance(app_paths.EnvFileEncodingError("x"), ValueError)
    [(listed, kind, detail)] = app_paths.unreadable_env_files([path, tmp_path / "missing.env"])
    assert listed == path and kind == "encoding" and str(path) in detail


def test_r12_1_the_launcher_skips_an_unreadable_file_and_loads_the_next(launcher, tmp_path, monkeypatch):
    """@brief Positive and negative in one order: the ANSI file is skipped, the UTF-16 one loaded."""
    pytest.importorskip("dotenv")
    bad = tmp_path / "first.env"
    bad.write_bytes("KICADGEN_C1_BAD=città\n".encode("cp1252"))
    good = tmp_path / "second.env"
    good.write_bytes("KICADGEN_C1_GOOD=sì\n".encode("utf-16"))
    for name in ("KICADGEN_C1_BAD", "KICADGEN_C1_GOOD"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    lines = []
    skipped = launcher.load_env_files([bad, good, tmp_path / "absent.env"], warn=lines.append)
    assert [(path, kind) for path, kind, _ in skipped] == [(bad, "encoding")]
    assert os.environ.get("KICADGEN_C1_GOOD") == "sì"
    assert "KICADGEN_C1_BAD" not in os.environ
    assert len(lines) == 1 and "first.env" in lines[0]


def test_r12_1_the_crash_hooks_exist_before_the_env_files_are_read(launcher, tmp_path, monkeypatch):
    """@brief Structural: a failure while reading .env now leaves a report.

    The order is observed by running main() with the GUI faked: when
    `load_env_files` runs, sys.excepthook is already the launcher's hook.
    """
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path))
    fake_gui = types.ModuleType("kicadgen.gui.main_window")
    fake_gui.main = lambda splash=None: 0
    monkeypatch.setitem(sys.modules, "kicadgen.gui.main_window", fake_gui)
    monkeypatch.setattr(launcher, "_project_python", lambda root: None)
    monkeypatch.setattr(launcher, "create_splash", lambda language: None)
    monkeypatch.setattr(launcher, "install_native_crash_log", lambda: None)
    monkeypatch.setattr(launcher.sys, "argv", ["main.py"])
    import threading
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    original_hook = sys.excepthook
    seen = []
    monkeypatch.setattr(launcher, "load_env_files", lambda: seen.append(sys.excepthook))
    try:
        assert launcher.main() == 0
    finally:
        crash_report.uninstall()
    assert len(seen) == 1 and seen[0] is not original_hook
    assert seen[0].__name__ == "report_and_stop"


# -- R12-2: a read error is not damage ------------------------------------------

def _sqlite(path):
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.executemany("INSERT INTO t VALUES (?)", [("x" * 200,)] * 50)
    connection.close()
    return path


@pytest.fixture
def frozen_seed(monkeypatch, tmp_path):
    """Installed layout with a real SQLite seed; returns (shipped, user target)."""
    for name in ("KICADGEN_DATA_DIR", "KICADGEN_CONFIG_PATH", "KICADGEN_PROFILE_DB"):
        monkeypatch.delenv(name, raising=False)
    bundle = tmp_path / "bundle"
    (bundle / "data").mkdir(parents=True)
    shipped = _sqlite(bundle / "data" / "analysis_profiles.sqlite3")
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    target = local / "KiCadGen" / "data" / "analysis_profiles.sqlite3"
    target.parent.mkdir(parents=True)
    return shipped, target


def test_r12_2_a_file_that_cannot_be_opened_is_not_declared_damaged(frozen_seed, monkeypatch):
    """@brief Positive: a sharing violation (backup, antivirus) leaves the user's database alone."""
    shipped, target = frozen_seed
    _sqlite(target)
    before = target.read_bytes()
    original_open = Path.open

    def busy_open(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "The process cannot access the file", str(self))
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", busy_open)
    assert app_paths.is_damaged_sqlite(target) is False
    assert app_paths.data_file("analysis_profiles.sqlite3", seed=True) == target
    monkeypatch.setattr(Path, "open", original_open)
    assert target.read_bytes() == before
    assert not list(target.parent.glob(target.name + ".corrupt-*"))


def test_r12_2_a_readable_wrong_header_is_still_damage(frozen_seed):
    """@brief Negative: bytes that were read and are wrong still set the file aside."""
    shipped, target = frozen_seed
    target.write_bytes(b"not a database at all, sixteen+ bytes")
    assert app_paths.is_damaged_sqlite(target) is True
    app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert target.read_bytes() == shipped.read_bytes()
    assert len(list(target.parent.glob(target.name + ".corrupt-*"))) == 1


# -- R12-9: the database and its companions move together or not at all --------

def test_r12_9_a_failing_companion_rename_puts_everything_back(tmp_path, monkeypatch):
    """@brief Positive: no seed is ever copied beside an orphan journal."""
    target = tmp_path / "db.sqlite3"
    target.write_bytes(b"damaged")
    journal = tmp_path / "db.sqlite3-journal"
    journal.write_bytes(b"stale journal")
    original_replace = os.replace

    def replace(source, destination):
        if str(source).endswith("-journal"):
            raise PermissionError(13, "in use", str(source))
        return original_replace(source, destination)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PermissionError):
        app_paths._set_aside(target)
    monkeypatch.setattr(os, "replace", original_replace)
    assert target.read_bytes() == b"damaged" and journal.read_bytes() == b"stale journal"
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_r12_9_without_failures_the_journal_follows_the_database(tmp_path):
    """@brief Negative: the normal case still moves both under one .corrupt- name."""
    target = tmp_path / "db.sqlite3"
    target.write_bytes(b"damaged")
    (tmp_path / "db.sqlite3-journal").write_bytes(b"stale journal")
    aside = app_paths._set_aside(target)
    assert not target.exists() and not (tmp_path / "db.sqlite3-journal").exists()
    assert aside.read_bytes() == b"damaged"
    assert aside.with_name(aside.name + "-journal").read_bytes() == b"stale journal"


# -- R12-3: check_runtime and the bounded smoke test ----------------------------

def test_r12_3_a_module_that_raises_at_import_fails_the_check_with_a_report(launcher, tmp_path, monkeypatch):
    """@brief Positive: a RuntimeError at import is a failed module, written to the report file."""
    (tmp_path / "c1_broken_module.py").write_text("raise RuntimeError('native library refused')\n",
                                                  encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    report = tmp_path / "check.txt"
    status = launcher.check_runtime(resource_path=lambda *parts: tmp_path / "x",
                                    modules=("c1_broken_module",), resources=(), qt_plugins=(),
                                    report_file=report)
    text = report.read_text(encoding="utf-8")
    assert status == 1
    assert "c1_broken_module: RuntimeError: native library refused" in text
    assert "Modules not importable: c1_broken_module" in text
    sys.modules.pop("c1_broken_module", None)


def test_r12_3_a_module_that_imports_passes_the_check(launcher, tmp_path, monkeypatch):
    """@brief Negative: the broadened catch does not fail a sound module."""
    (tmp_path / "c1_sound_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    status = launcher.check_runtime(resource_path=lambda *parts: tmp_path / "x",
                                    modules=("c1_sound_module",), resources=(), qt_plugins=())
    assert status == 0
    sys.modules.pop("c1_sound_module", None)


def test_r12_3_the_release_build_step_has_its_own_bounded_timeout():
    """@brief The build step is bounded and the job limit stays above the sum of the step limits."""
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["build"]
    build = next(step for step in job["steps"] if "build-windows.ps1" in str(step.get("run", "")))
    assert build["timeout-minutes"] > 0
    # Follow-up 3: EVERY step has a limit (uploads, checkout and setup
    # included), so the sum below is the whole job's worst case...
    assert [step.get("name", step.get("uses")) for step in job["steps"]
            if "timeout-minutes" not in step] == []
    # ...and the job limit stays above it, so a step limit, never the job
    # limit, is what stops a slow step.
    bounded = sum(step["timeout-minutes"] for step in job["steps"])
    assert job["timeout-minutes"] >= bounded
    # The comment's arithmetic is the file's (it states the sum it relies on).
    assert f"add up to {bounded} min" in (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")


def _powershell():
    """Windows PowerShell 5.1 (the edition R12-8 is about), or None."""
    return shutil.which("powershell")


def _run_script_functions(script, names, body, cwd=None, timeout=120):
    """Define functions of a repository script through the PowerShell AST, then run `body`."""
    path = ROOT / "scripts" / script
    loader = "".join(
        "$f=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        f" -and $n.Name -eq '{name}'}},$true); . ([scriptblock]::Create($f.Extent.Text)); "
        for name in names)
    command = (f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$null,[ref]$null); "
               + loader + "$ErrorActionPreference='Stop'; " + body)
    return subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=timeout, cwd=cwd)


@pytest.mark.skipif(_powershell() is None, reason="PowerShell non disponibile")
def test_r12_3_the_smoke_test_returns_the_exit_code_of_a_program_that_ends(tmp_path):
    """@brief Negative of the timeout: a program that ends in time gives back its own exit code."""
    result = _run_script_functions(
        "build-windows.ps1", ["ConvertTo-CommandLineArgument", "Invoke-WithTimeout"],
        f"$code = Invoke-WithTimeout '{sys.executable}' @('-c','import sys; sys.exit(3)') 60; "
        "Write-Output \"exit=$code\"")
    assert "exit=3" in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(_powershell() is None, reason="PowerShell non disponibile")
def test_r12_3_arguments_reach_the_program_unchanged(tmp_path):
    """@brief Start-Process joins arguments with bare spaces: each one must be quoted to survive."""
    out = tmp_path / "argv.json"
    program = f"import json, sys; open(r'{out}', 'w').write(json.dumps(sys.argv[1:]))"
    result = _run_script_functions(
        "build-windows.ps1", ["ConvertTo-CommandLineArgument", "Invoke-WithTimeout"],
        f"$code = Invoke-WithTimeout '{sys.executable}' @('-c', \"{program}\", 'two words', "
        "'say \"hi\"', 'C:\\dir\\', 'plain', '') 60; Write-Output \"exit=$code\"")
    assert "exit=0" in result.stdout, result.stdout + result.stderr
    assert json.loads(out.read_text()) == ["two words", 'say "hi"', "C:\\dir\\", "plain", ""]


@pytest.mark.skipif(_powershell() is None, reason="PowerShell non disponibile")
def test_r12_3_a_hanging_smoke_test_is_killed_and_fails(tmp_path):
    """@brief Positive: past the bound the process tree is killed and the build throws."""
    marker = tmp_path / "pid.txt"
    script = f"import os, time; open(r'{marker}', 'w').write(str(os.getpid())); time.sleep(120)"
    started = time.monotonic()
    result = _run_script_functions(
        "build-windows.ps1", ["ConvertTo-CommandLineArgument", "Invoke-WithTimeout"],
        f"Invoke-WithTimeout '{sys.executable}' @('-c', \"{script}\") 3")
    assert result.returncode != 0
    assert "did not finish within 3 s" in result.stdout + result.stderr
    assert time.monotonic() - started < 90
    pid = int(marker.read_text())
    alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    assert str(pid) not in alive.stdout


# -- R12-8: native stderr under 'Stop' in Windows PowerShell 5.1 ----------------

_STDERR_PROGRAM = "import sys; sys.stderr.write('a warning'); print('True')"


@pytest.mark.skipif(_powershell() is None, reason="PowerShell non disponibile")
def test_r12_8_invoke_native_tolerates_a_warning_on_stderr():
    """@brief Positive: through Invoke-Native the answer on stdout is read, the warning ignored."""
    result = _run_script_functions(
        "build-windows.ps1", ["Invoke-Native"],
        f"$answer = Invoke-Native {{ & '{sys.executable}' -c \"{_STDERR_PROGRAM}\" 2>$null }}; "
        "Write-Output \"answer=$answer\"")
    assert result.returncode == 0 and "answer=True" in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(_powershell() is None, reason="PowerShell non disponibile")
def test_r12_8_without_invoke_native_the_same_warning_aborts_under_stop():
    """@brief Negative: the defect the helper removes, reproduced on the same PowerShell."""
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command",
         f"$ErrorActionPreference='Stop'; $answer = & '{sys.executable}' -c \"{_STDERR_PROGRAM}\" 2>$null; "
         "Write-Output \"answer=$answer\""],
        capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        pytest.skip("this PowerShell does not turn native stderr into errors (PowerShell 7.2+)")
    assert "answer=True" not in result.stdout


def test_r12_8_every_native_call_of_the_build_goes_through_invoke_native():
    """@brief Structural: no bare `& $python` / `& $base` call is left outside the helper."""
    lines = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8").splitlines()
    bare = [line for line in lines
            if line.lstrip().startswith(("& $python", "& $base", "& $candidate"))]
    assert bare == []
    assert sum("Invoke-Native {" in line for line in lines) >= 8


# -- R12-7: publish-release -RunTestsLocally refuses untracked files ------------

@pytest.mark.skipif(_powershell() is None or shutil.which("git") is None,
                    reason="PowerShell o git non disponibili")
def test_r12_7_untracked_files_stop_the_local_test_gate(tmp_path):
    """@brief Positive: a new file outside Git would be tested locally but missing from the tag."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    result = _run_script_functions("publish-release.ps1", ["Assert-LastCommand", "Assert-NoUntrackedFiles"],
                                   "Assert-NoUntrackedFiles; Write-Output 'clean'", cwd=tmp_path)
    assert result.returncode != 0 and "clean" not in result.stdout
    assert "new_module.py" in result.stdout + result.stderr


@pytest.mark.skipif(_powershell() is None or shutil.which("git") is None,
                    reason="PowerShell o git non disponibili")
def test_r12_7_ignored_files_do_not_stop_the_gate(tmp_path):
    """@brief Negative: caches and environments are ignored by design and never block."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / ".gitignore").write_text("cache/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore"], check=True, timeout=60)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "x.bin").write_bytes(b"x")
    result = _run_script_functions("publish-release.ps1", ["Assert-LastCommand", "Assert-NoUntrackedFiles"],
                                   "Assert-NoUntrackedFiles; Write-Output 'clean'", cwd=tmp_path)
    assert result.returncode == 0 and "clean" in result.stdout, result.stdout + result.stderr


def test_r12_7_the_local_run_checks_untracked_files_before_testing():
    """@brief Structural: the check is the first thing the local test run does."""
    script = (ROOT / "scripts" / "publish-release.ps1").read_text(encoding="utf-8")
    body = script.split("function Invoke-TestsLocally() {", 1)[1]
    assert body.index("Assert-NoUntrackedFiles") < body.index("& $python")
    assert "--untracked-files=no" in script  # the tracked-changes check is unchanged


# -- R12-5: native crashes leave a report ---------------------------------------

def test_r12_5_the_native_log_is_empty_unlisted_and_removed_at_a_normal_exit(tmp_path, native_clean):
    """@brief Negative: a session without a crash leaves nothing behind and lists nothing."""
    path = crash_report.open_native_log(tmp_path, enable_faulthandler=False)
    assert path is not None and path.exists() and path.stat().st_size == 0
    assert crash_report.open_native_log(tmp_path, enable_faulthandler=False) == path
    assert crash_report.latest_reports(base=tmp_path) == []
    assert crash_report.adopt_native_logs(tmp_path) == []  # its own log is never adopted
    crash_report.discard_native_log()
    assert not path.exists()


def test_r12_5_a_log_left_by_a_dead_process_becomes_a_scrubbed_report(tmp_path, native_clean):
    """@brief Positive: the next start turns the leftover into a report the menu names."""
    folder = crash_report.reports_dir(tmp_path)
    folder.mkdir(parents=True)
    home = str(Path.home())
    dead = folder / "native-20260101-000000-000000-999999.log"
    dead.write_text(f"Qt fatal: could not load {home}\\plugin.dll\nWindows fatal exception: access violation\n",
                    encoding="utf-8")
    empty = folder / "native-20260101-000001-000000-999998.log"
    empty.write_text("", encoding="utf-8")
    [report] = crash_report.adopt_native_logs(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert report.name.endswith("-native.log") and report.name.startswith("crash-")
    assert "access violation" in text and "native crash" in text
    assert home not in text  # scrubbed like every report
    assert not dead.exists() and not empty.exists()
    assert crash_report.latest_reports(base=tmp_path) == [report]


@pytest.mark.skipif(os.name != "nt", reason="liveness by open handle is the Windows path")
def test_r12_5_the_log_of_a_running_process_is_left_alone(tmp_path, native_clean):
    """@brief Negative: a log held open (another running instance) is neither deleted nor adopted."""
    folder = crash_report.reports_dir(tmp_path)
    folder.mkdir(parents=True)
    live = folder / "native-20260101-000000-000000-1234.log"
    with open(live, "w", encoding="utf-8") as holder:
        holder.write("Qt critical: something\n")
        holder.flush()
        assert crash_report.adopt_native_logs(tmp_path) == []
        assert live.exists()
    assert crash_report.latest_reports(base=tmp_path) == []


def test_r12_5_a_real_native_crash_is_reported_at_the_next_start(tmp_path, native_clean):
    """@brief End to end: a child process crashes in C, the next start finds its stacks."""
    child = ("import faulthandler, sys; sys.path.insert(0, sys.argv[2]); "
             "from kicadgen import crash_report; "
             "assert crash_report.open_native_log(sys.argv[1]) is not None; "
             "faulthandler._sigsegv()")
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONFAULTHANDLER",)}
    result = subprocess.run([sys.executable, "-c", child, str(tmp_path), str(ROOT)],
                            capture_output=True, text=True, timeout=120, env=environment)
    assert result.returncode != 0
    [report] = crash_report.adopt_native_logs(tmp_path)
    text = report.read_text(encoding="utf-8")
    assert "fatal" in text.lower() and "File " in text, text


def test_r12_5_only_critical_and_fatal_qt_messages_reach_the_native_log(launcher):
    """@brief Positive and negative: failures are kept, warnings and info are not."""
    assert launcher.qt_message_line("QtCriticalMsg", "boom") == "Qt critical: boom"
    assert launcher.qt_message_line("QtMsgType.QtFatalMsg", "abort") == "Qt fatal: abort"
    for kind in ("QtWarningMsg", "QtInfoMsg", "QtDebugMsg"):
        assert launcher.qt_message_line(kind, "noise") is None


def test_r12_5_the_qt_handler_chains_to_the_previous_one_and_gives_it_back(launcher, tmp_path, native_clean):
    """@brief Positive: the previous handler still sees every message; at exit it is reinstalled."""
    QtCore = pytest.importorskip("PyQt6.QtCore")
    received = []

    def recorder(kind, context, message):
        received.append((kind.name, message))
    saved = QtCore.qInstallMessageHandler(recorder)
    try:
        path = crash_report.open_native_log(tmp_path, enable_faulthandler=False)
        exits = []
        assert launcher.install_qt_message_log(register_exit=exits.append) is True
        handler = launcher._QT_MESSAGE_HANDLER
        handler(QtCore.QtMsgType.QtWarningMsg, None, "just a warning")
        handler(QtCore.QtMsgType.QtCriticalMsg, None, "a real failure")
        assert received == [("QtWarningMsg", "just a warning"), ("QtCriticalMsg", "a real failure")]
        assert path.read_text(encoding="utf-8") == "Qt critical: a real failure\n"
        [restore] = exits
        restore()
        assert QtCore.qInstallMessageHandler(recorder) is recorder
    finally:
        QtCore.qInstallMessageHandler(saved)


def test_r12_5_the_python_crash_hook_drops_the_native_log(launcher, tmp_path, monkeypatch, native_clean):
    """@brief Negative: a crash with its own Python report leaves no second, native one."""
    import threading
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    exits = []
    hook = launcher.install_crash_reports(exit_process=exits.append)
    native = crash_report.open_native_log(enable_faulthandler=False)
    assert native is not None and native.exists()
    try:
        raise RuntimeError("c1 test failure")
    except RuntimeError:
        hook(*sys.exc_info())
    assert exits == [1] and not native.exists()
    reports = crash_report.latest_reports(base=tmp_path / "logs")
    assert len(reports) == 1 and not reports[0].name.endswith("-native.log")


def test_r12_5_an_existing_faulthandler_is_not_taken_over(tmp_path, native_clean):
    """@brief Negative: `-X faulthandler` or pytest chose a destination; it is kept and not disabled."""
    import faulthandler
    if not faulthandler.is_enabled():
        pytest.skip("faulthandler is not enabled in this session")
    path = crash_report.open_native_log(tmp_path)
    assert path is not None
    crash_report.discard_native_log()
    assert faulthandler.is_enabled()


# -- R12-6: a cut transfer is retried, a different file is not ------------------

@pytest.fixture(scope="module")
def weights_tool():
    """tools/fetch_model_weights.py loaded by path."""
    return _load("tools/fetch_model_weights.py", "fetch_model_weights_c1")


class _Body:
    """A response with optional headers, usable as a context manager."""

    def __init__(self, payload, length=None):
        import io
        self._stream = io.BytesIO(payload)
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _manifest_entry(payload):
    import hashlib
    return {"file": "w.onnx", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://example.invalid/w.onnx"}


def test_r12_6_a_body_cut_short_of_its_announced_length_is_transient(weights_tool, tmp_path, monkeypatch, capsys):
    """@brief Positive: the server announced the recorded size and sent less: exit 1, retryable."""
    payload = b"0123456789" * 10
    entry = _manifest_entry(payload)
    with pytest.raises(weights_tool.TruncatedDownloadError):
        weights_tool.fetch(entry, tmp_path, opener=lambda url: _Body(payload[:40], len(payload)),
                           log=lambda line: None)
    assert not list((tmp_path / "yolo").iterdir())
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [entry]}), encoding="utf-8")
    monkeypatch.setattr(weights_tool.urllib.request, "urlopen",
                        lambda url, timeout=None: _Body(payload[:40], len(payload)))
    assert weights_tool.main(["--manifest", str(manifest), "--target", str(tmp_path / "models")]) == 1
    assert "truncated" in capsys.readouterr().err


def test_r12_6_a_complete_shorter_file_is_permanent(weights_tool, tmp_path, monkeypatch):
    """@brief Negative: the server announced exactly what it sent: another asset, exit 2."""
    payload = b"0123456789" * 10
    entry = _manifest_entry(payload)
    short = payload[:40]
    with pytest.raises(ValueError, match="complete file of 40 bytes"):
        weights_tool.fetch(entry, tmp_path, opener=lambda url: _Body(short, len(short)),
                           log=lambda line: None)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [entry]}), encoding="utf-8")
    monkeypatch.setattr(weights_tool.urllib.request, "urlopen",
                        lambda url, timeout=None: _Body(short, len(short)))
    assert weights_tool.main(["--manifest", str(manifest), "--target", str(tmp_path / "models")]) \
        == weights_tool.EXIT_PERMANENT


@pytest.mark.parametrize("headers,expected", [
    ({"Content-Length": "12"}, 12), ({"Content-Length": " 7 "}, 7),
    ({"Content-Length": "abc"}, None), ({"Content-Length": "-1"}, None), ({}, None), (None, None)])
def test_r12_6_the_announced_length_is_read_only_when_it_is_a_byte_count(weights_tool, headers, expected):
    response = types.SimpleNamespace(headers=headers) if headers is not None else object()
    assert weights_tool.declared_length(response) == expected


# -- R12-10: a bounded half -----------------------------------------------------

@pytest.fixture(scope="module")
def halves_tool():
    """tools/run_test_halves.py loaded by path."""
    return _load("tools/run_test_halves.py", "run_test_halves_c1")


def test_r12_10_a_half_past_its_bound_is_killed_with_its_children(halves_tool, tmp_path):
    """@brief Positive: the half and the process it started are gone, with the timeout status."""
    marker = tmp_path / "grandchild.txt"
    grandchild = f"import os, time; open(r'{marker}', 'w').write(str(os.getpid())); time.sleep(120)"
    parent = (f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
              "time.sleep(120)")
    started = time.monotonic()
    status = halves_tool.run_half([sys.executable, "-c", parent], timeout_s=5)
    assert status == halves_tool.HALF_TIMEOUT_EXIT
    assert time.monotonic() - started < 90
    pid = int(marker.read_text())
    if os.name == "nt":
        alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        assert str(pid) not in alive.stdout
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_r12_10_a_half_that_ends_in_time_keeps_its_own_status(halves_tool):
    """@brief Negative: the bound changes nothing for a half that finishes."""
    assert halves_tool.run_half([sys.executable, "-c", "import sys; sys.exit(3)"], timeout_s=60) == 3
    assert halves_tool.run_half([sys.executable, "-c", "pass"]) == 0


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "x"])
def test_r12_10_a_bad_timeout_is_a_usage_error(halves_tool, value):
    assert halves_tool.main(["first", "--timeout", value]) == 2
    assert halves_tool.main(["first", "--timeout"]) == 2



# -- Follow-up 1: a dumped exception the process survived is not a crash -------

_FAKE_DUMP = ("Windows fatal exception: code 0x8001010d\n\n"
              "Thread 0x00001234 (most recent call first):\n  File \"x.py\", line 1 in f\n")


def test_followup1_a_dump_followed_by_the_continuation_mark_is_not_a_crash():
    """@brief Negative: lines above the mark were survived, and a tail without a fatal line is a kill."""
    survived = _FAKE_DUMP + crash_report.NATIVE_CONTINUED + "\n"
    assert crash_report.native_crash_text(survived) == ""
    assert crash_report.native_crash_text(survived + "Qt critical: lost device\n") == ""
    assert crash_report.native_crash_text("") == ""


def test_followup1_a_fatal_tail_after_the_last_mark_is_a_crash_without_the_survived_blocks():
    """@brief Positive: only the unsurvived tail is reported, the earlier false blocks are dropped."""
    text = (_FAKE_DUMP + crash_report.NATIVE_CONTINUED + "\n"
            + "Qt critical: before the end\nFatal Python error: Aborted\n\nCurrent thread 0x1:\n")
    tail = crash_report.native_crash_text(text)
    assert tail.startswith("Qt critical: before the end") and "Fatal Python error" in tail
    assert "0x8001010d" not in tail
    assert crash_report.native_crash_text(_FAKE_DUMP) == _FAKE_DUMP.strip("\n")


def test_followup1_the_watch_marks_only_after_the_process_outlived_the_growth(tmp_path):
    """@brief Positive after the grace, negative before it and without growth."""
    path = tmp_path / "log.txt"
    # newline="\n" as open_native_log: the mark's byte count is checked (review G).
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        watch = crash_report.NativeContinuationWatch(handle, grace_s=30)
        assert watch.poll(0.0) is False                  # empty: nothing to survive
        handle.write(_FAKE_DUMP)
        handle.flush()
        assert watch.poll(1.0) is False                  # growth seen now
        assert watch.poll(30.9) is False                 # not yet survived
        assert watch.poll(31.0) is True                  # survived: mark written
        assert watch.poll(500.0) is False                # no new growth, no second mark
    text = path.read_text(encoding="utf-8")
    assert text.count(crash_report.NATIVE_CONTINUED) == 1
    assert crash_report.native_crash_text(text) == ""


def test_followup1_orderly_ends_of_the_application_mark_the_log(tmp_path, native_clean):
    """@brief aboutToQuit / commitDataRequest write the mark; an app is connected once."""
    class _Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

    app = types.SimpleNamespace(aboutToQuit=_Signal(), commitDataRequest=_Signal())
    assert crash_report.watch_application(app) is True
    assert crash_report.watch_application(app) is False
    assert len(app.aboutToQuit.slots) == 1 and len(app.commitDataRequest.slots) == 1
    path = crash_report.open_native_log(tmp_path, enable_faulthandler=False, watch=False)
    crash_report.log_native_line("Windows fatal exception: code 0x8001010d")
    app.commitDataRequest.slots[0](object())          # session end: Qt passes a manager
    text = path.read_text(encoding="utf-8")
    # CONTRACT CHANGED 2026-11-16 (crash notice): an orderly end writes its
    # own mark, NATIVE_ORDERLY_END, which also tells a logoff from a kill.
    assert text.rstrip("\n").endswith(crash_report.NATIVE_ORDERLY_END)
    assert crash_report.native_crash_text(text) == ""
    # Negative: an object without the signals is not "watched".
    assert crash_report.watch_application(object()) is False


_HANDLED_CHILD = r'''
import ctypes, os, sys, time
sys.path.insert(0, sys.argv[2])
from kicadgen import crash_report
path = crash_report.open_native_log(sys.argv[1], poll_s=0.05, grace_s=0.5)
assert path is not None
try:
    ctypes.string_at(1)
except OSError:
    pass
time.sleep(2.0)
print(open(path, encoding="utf-8").read(), flush=True)
os._exit(0)
'''


@pytest.mark.skipif(os.name != "nt", reason="first-chance dumps are a Windows faulthandler behaviour")
def test_followup1_a_handled_access_violation_then_a_kill_leaves_no_report(tmp_path, native_clean):
    """@brief End to end: ctypes catches the access violation faulthandler dumped; no false report.

    The child survives the exception longer than its (shortened) grace,
    then ends with os._exit, like a kill: no atexit runs.
    """
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONFAULTHANDLER"}
    result = subprocess.run([sys.executable, "-c", _HANDLED_CHILD, str(tmp_path), str(ROOT)],
                            capture_output=True, text=True, timeout=120, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Windows fatal exception" in result.stdout     # the dump really happened
    assert crash_report.NATIVE_CONTINUED in result.stdout
    assert crash_report.adopt_native_logs(tmp_path) == []
    assert not list(crash_report.reports_dir(tmp_path).glob("native-*.log"))


# -- Follow-up 2: the check report is printed on a timeout too -----------------

@pytest.mark.skipif(_powershell() is None, reason="PowerShell non disponibile")
def test_followup2_show_check_report_prints_the_file_or_says_there_is_none(tmp_path):
    report = tmp_path / "check.txt"
    report.write_text("PyQt6: OK\nkicadgen.gui.main_window: hung here\n", encoding="utf-8")
    shown = _run_script_functions("build-windows.ps1", ["Show-CheckReport"],
                                  f"Show-CheckReport '{report}'; Show-CheckReport '{tmp_path / 'none.txt'}'")
    assert "hung here" in shown.stdout and "wrote no report" in shown.stdout, shown.stdout + shown.stderr


def test_followup2_a_timed_out_check_prints_the_report_before_failing():
    """@brief Structural: the catch around the bounded check prints the report, then rethrows."""
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    block = build.split("$checkExit = Invoke-WithTimeout", 1)[1].split("} finally {", 1)[0]
    catch = block.split("} catch {", 1)[1]
    assert catch.index("Show-CheckReport $checkReport") < catch.index("throw")


# -- Follow-up 4: a worker failing after the quit began is not a crash ---------

@pytest.fixture
def quit_state(monkeypatch):
    """A stand-in for kicadgen.gui.quit_wait whose is_quitting() the test sets."""
    module = types.ModuleType("kicadgen.gui.quit_wait")
    module.quitting = False
    module.is_quitting = lambda: module.quitting
    monkeypatch.setitem(sys.modules, "kicadgen.gui.quit_wait", module)
    return module


def _hook_in_thread(hook, main_thread=False):
    """Raise and hand the exception to `hook`, from a worker thread unless `main_thread`."""
    import threading

    def fail():
        try:
            raise RuntimeError("late worker failure")
        except RuntimeError:
            hook(*sys.exc_info())
    if main_thread:
        fail()
        return
    worker = threading.Thread(target=fail, name="c1-late-worker")
    worker.start()
    worker.join()


@pytest.fixture
def launcher_hook(launcher, tmp_path, monkeypatch, native_clean):
    """The launcher's crash hook writing into a throwaway data folder; exits recorded."""
    import threading
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    exits = []
    hook = launcher.install_crash_reports(exit_process=exits.append)
    return hook, exits, tmp_path / "logs"


def test_followup4_a_worker_failing_after_the_quit_is_printed_not_reported(launcher_hook, quit_state, capsys):
    """@brief Positive: no report, no exit status 1, the traceback still on stderr."""
    hook, exits, logs = launcher_hook
    quit_state.quitting = True
    _hook_in_thread(hook)
    assert exits == [] and crash_report.latest_reports(base=logs) == []
    assert "late worker failure" in capsys.readouterr().err


@pytest.mark.parametrize("quitting,main_thread", [(False, False), (True, True)],
                         ids=["worker-before-quit", "main-thread-while-quitting"])
def test_followup4_other_failures_are_still_reported_and_stop(launcher_hook, quit_state, quitting, main_thread):
    """@brief Negative: before the quit, or on the main thread, the report and the exit stay."""
    hook, exits, logs = launcher_hook
    quit_state.quitting = quitting
    _hook_in_thread(hook, main_thread=main_thread)
    assert exits == [1] and len(crash_report.latest_reports(base=logs)) == 1


def test_followup4_without_the_quit_module_the_hook_reports_as_before(launcher_hook, monkeypatch):
    """@brief Negative: a GUI that never loaded quit_wait (or lacks is_quitting) is not quitting."""
    hook, exits, logs = launcher_hook
    monkeypatch.delitem(sys.modules, "kicadgen.gui.quit_wait", raising=False)
    _hook_in_thread(hook)
    monkeypatch.setitem(sys.modules, "kicadgen.gui.quit_wait", types.ModuleType("kicadgen.gui.quit_wait"))
    _hook_in_thread(hook)
    assert exits == [1, 1] and len(crash_report.latest_reports(base=logs)) == 2


# -- Final review E: one writer at a time, and a tolerant mark -----------------

def test_review_e_concurrent_writers_keep_every_mark_line_intact(tmp_path, native_clean):
    """@brief Positive: Qt-handler lines and continuation marks from many threads never interleave."""
    import threading
    path = crash_report.open_native_log(tmp_path, enable_faulthandler=False, watch=False)
    payload = "Qt critical: " + "x" * 3000
    start = threading.Barrier(8)

    def qt_lines():
        start.wait()
        for _ in range(200):
            crash_report.log_native_line(payload)

    def marks():
        start.wait()
        for _ in range(200):
            crash_report.mark_native_continued()
    threads = [threading.Thread(target=qt_lines if index % 2 else marks) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 200
    assert lines.count(crash_report.NATIVE_CONTINUED) == 4 * 200
    assert lines.count(payload) == 4 * 200


def test_review_e_a_whole_mark_after_a_foreign_prefix_still_counts():
    """@brief Positive: a mark glued after an unfinished faulthandler line keeps the blocks above survived."""
    text = (_FAKE_DUMP + "  File \"y.py\", line 2 in g" + crash_report.NATIVE_CONTINUED + "\n"
            + "Qt critical: harmless\n")
    assert crash_report.native_crash_text(text) == ""
    # What follows the mark on its own line starts the tail.
    glued = _FAKE_DUMP + crash_report.NATIVE_CONTINUED + "Fatal Python error: Aborted\n"
    assert crash_report.native_crash_text(glued) == "Fatal Python error: Aborted"


def test_review_e_a_cut_mark_is_not_recognised_and_errs_towards_a_report():
    """@brief Negative: half a mark proves nothing, so the dump above it counts again."""
    cut = crash_report.NATIVE_CONTINUED[:20]
    text = _FAKE_DUMP + cut + "\n"
    assert "Windows fatal exception" in crash_report.native_crash_text(text)


# -- User decision 16/11: the crash notice at the next start -------------------

@pytest.fixture
def fresh_sessions(monkeypatch, native_clean):
    """Start each test with no kill counted by an earlier adoption."""
    monkeypatch.setitem(crash_report._state, "unclean_sessions", 0)


def _python_report(base, stamp):
    """A Python crash report as `write_report` names it, at a chosen time."""
    folder = crash_report.reports_dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"crash-{stamp}-4242.log"
    path.write_text("KiCadGen crash report\n", encoding="utf-8")
    return path


def test_notice_a_crash_report_is_shown_once_then_only_a_newer_one(tmp_path, fresh_sessions):
    """@brief Positive: an unseen report is returned; once marked it is not, until a newer one comes."""
    assert crash_report.unseen_crash_report(tmp_path) is None          # no report: no notice
    first = _python_report(tmp_path, "20261116-100000-000000")
    assert crash_report.unseen_crash_report(tmp_path) == first
    assert crash_report.mark_crash_report_seen(first, tmp_path) is True
    assert crash_report.unseen_crash_report(tmp_path) is None
    second = _python_report(tmp_path, "20261116-110000-000000")
    assert crash_report.unseen_crash_report(tmp_path) == second
    # Negative: marking an older report never moves the stamp back.
    assert crash_report.mark_crash_report_seen(second, tmp_path)
    assert crash_report.mark_crash_report_seen(first, tmp_path)
    assert crash_report.unseen_crash_report(tmp_path) is None
    # The stamp is not a report and is never listed.
    assert all(path.name.startswith("crash-") for path in crash_report.latest_reports(base=tmp_path))


def test_notice_a_native_crash_adopted_at_start_is_unseen(tmp_path, fresh_sessions):
    """@brief Positive: the report made from a native log is the one the notice names."""
    first = _python_report(tmp_path, "20250101-000000-000000")
    crash_report.mark_crash_report_seen(first, tmp_path)
    leftover = crash_report.reports_dir(tmp_path) / "native-20261116-090000-000000-777.log"
    leftover.write_text("Windows fatal exception: access violation\n", encoding="utf-8")
    [adopted] = crash_report.adopt_native_logs(tmp_path)
    assert crash_report.unseen_crash_report(tmp_path) == adopted
    assert crash_report.sessions_ended_without_exit() == 0     # a crash is not a kill


def test_notice_a_kill_is_counted_and_an_orderly_end_or_a_normal_exit_is_not(tmp_path, fresh_sessions):
    """@brief Kill: leftover without exit/crash/orderly end. Negative: logoff mark, and a normal exit."""
    folder = crash_report.reports_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "native-20261116-080000-000000-701.log").write_text("", encoding="utf-8")
    (folder / "native-20261116-080001-000000-702.log").write_text(
        _FAKE_DUMP + crash_report.NATIVE_CONTINUED + "\n", encoding="utf-8")
    (folder / "native-20261116-080002-000000-703.log").write_text(
        _FAKE_DUMP + crash_report.NATIVE_ORDERLY_END + "\n", encoding="utf-8")
    assert crash_report.adopt_native_logs(tmp_path) == []
    assert crash_report.sessions_ended_without_exit() == 2
    assert crash_report.unseen_crash_report(tmp_path) is None   # a kill has no report to show
    # A normal exit: the log is deleted at atexit, nothing is left to count.
    path = crash_report.open_native_log(tmp_path, enable_faulthandler=False, watch=False)
    crash_report.discard_native_log()
    assert not path.exists()
    assert crash_report.adopt_native_logs(tmp_path) == []
    assert crash_report.sessions_ended_without_exit() == 2
    assert crash_report.unseen_crash_report(tmp_path) is None


# -- Review G: a mark that overlapped a crash dump is voided --------------------

_FATAL_BYTES = b"Fatal Python error: Segmentation fault\n\nCurrent thread 0x1 (most recent call first):\n"


class _DumpDuringMark:
    """A native-log handle that lets a 'faulthandler' write land while the mark is written.

    faulthandler writes to the descriptor without the module's lock; here
    those bytes go straight to the descriptor just before the mark's own
    buffered write, as a dump starting between the two size reads would.
    """

    def __init__(self, handle, inject):
        self._handle = handle
        self._inject = inject

    def fileno(self):
        return self._handle.fileno()

    def write(self, text):
        if self._inject and crash_report.NATIVE_CONTINUED in text:
            self._inject = False
            os.write(self._handle.fileno(), _FATAL_BYTES)
        return self._handle.write(text)

    def flush(self):
        self._handle.flush()


def _survived_watch(tmp_path, inject):
    """A watch whose log grew, then stayed unchanged past the grace; the next poll marks."""
    path = tmp_path / "native.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0))
    raw = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
    handle = _DumpDuringMark(raw, inject)
    watch = crash_report.NativeContinuationWatch(handle, grace_s=10)
    raw.write(_FAKE_DUMP)
    raw.flush()
    assert watch.poll(0.0) is False
    return path, raw, watch


def test_review_g_a_dump_overlapping_the_mark_voids_it_and_stays_a_crash(tmp_path):
    """@brief Positive: the fatal dump written during the mark is reported, the mark voided."""
    path, raw, watch = _survived_watch(tmp_path, inject=True)
    try:
        assert watch.poll(20.0) is False
    finally:
        raw.close()
    text = path.read_text(encoding="utf-8")
    assert crash_report.NATIVE_MARK_VOID in text
    tail = crash_report.native_crash_text(text)
    assert "Fatal Python error: Segmentation fault" in tail
    # The voided mark lets the earlier, survived-looking dump count again: the
    # safe side, a report rather than a crash counted as a kill.
    assert "0x8001010d" in tail


def test_review_g_a_mark_without_overlap_stands(tmp_path):
    """@brief Negative: the same poll with no concurrent write leaves a valid mark and no void."""
    path, raw, watch = _survived_watch(tmp_path, inject=False)
    try:
        assert watch.poll(20.0) is True
    finally:
        raw.close()
    text = path.read_text(encoding="utf-8")
    assert crash_report.NATIVE_MARK_VOID not in text
    assert crash_report.native_crash_text(text) == ""


def test_review_g_a_dump_finished_before_the_lock_prevents_the_mark(tmp_path, monkeypatch):
    """@brief Positive: growth between the survival judgement and the lock means no mark at all."""
    path, raw, watch = _survived_watch(tmp_path, inject=False)
    original_fstat = os.fstat
    calls = []

    def fstat(fd):
        calls.append(fd)
        if len(calls) == 2:          # the first size read under the lock
            os.write(raw.fileno(), _FATAL_BYTES)
        return original_fstat(fd)
    monkeypatch.setattr(os, "fstat", fstat)
    try:
        assert watch.poll(20.0) is False
    finally:
        monkeypatch.setattr(os, "fstat", original_fstat)
        raw.close()
    text = path.read_text(encoding="utf-8")
    assert crash_report.NATIVE_CONTINUED not in text
    assert "Fatal Python error" in crash_report.native_crash_text(text)


def test_review_g_a_void_cancels_only_the_last_standing_mark():
    """@brief The earlier valid mark keeps the blocks above it survived."""
    text = ("Windows fatal exception: code 0x1\n" + crash_report.NATIVE_CONTINUED + "\n"
            + "Qt critical: x\n" + crash_report.NATIVE_CONTINUED + "\n" + crash_report.NATIVE_MARK_VOID + "\n"
            + "Fatal Python error: Aborted\n")
    tail = crash_report.native_crash_text(text)
    assert tail.startswith("Qt critical: x") and "Fatal Python error: Aborted" in tail
    assert "code 0x1" not in tail
