"""@brief Tests of the test harness itself (tests/conftest.py, pytest.ini).

The harness turns native aborts into test failures (HANDOFF 113, 131 block
B). Its failure paths are checked in a child pytest process, so a harness
defect cannot abort the main run: each child runs a tiny test file with the
real conftest and the real pytest.ini, and the outcome is read from its
summary line and exit code.
"""
import os
import socket
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

import pytest

import conftest as harness

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

#: Loader placed next to the child's test file: it executes the real
#: conftest in that folder's namespace, so its fixtures and hooks apply.
_LOADER = textwrap.dedent(f"""
    import importlib.util, sys
    for entry in ({str(TESTS)!r}, {str(REPO)!r}):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    _spec = importlib.util.spec_from_file_location("kicadgen_test_harness", {str(TESTS / "conftest.py")!r})
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["kicadgen_test_harness"] = _module
    _spec.loader.exec_module(_module)
    globals().update({{k: v for k, v in vars(_module).items() if not k.startswith("__")}})
""")


def run_child(tmp_path, body: str) -> subprocess.CompletedProcess:
    """@brief Run one test file under the real harness in a separate pytest process.

    @param tmp_path Folder for the child's files.
    @param body Source of the child's test module.
    @return The completed process (stdout holds the pytest report).
    """
    folder = tmp_path / "child"
    folder.mkdir()
    (folder / "conftest.py").write_text(_LOADER, encoding="utf-8")
    (folder / "test_child.py").write_text(textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(REPO / "pytest.ini"), "--rootdir", str(folder),
         "--confcutdir", str(folder), "--basetemp", str(tmp_path / "child-tmp"),
         str(folder / "test_child.py")],
        cwd=folder, env=env, capture_output=True, text=True, timeout=240)


def outcome(result: subprocess.CompletedProcess) -> str:
    """Return the child's stdout and stderr, for assertion messages."""
    return f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"


_QT_APP = """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QRunnable, QThread, QThreadPool, QTimer
    from PyQt6.QtWidgets import QApplication
    import pytest

    _APP = QApplication.instance() or QApplication([])

    def spin(ms=100):
        loop_end = []
        QTimer.singleShot(ms, lambda: loop_end.append(True))
        while not loop_end:
            _APP.processEvents()
"""


def test_exception_in_timer_slot_fails_only_that_test(tmp_path):
    result = run_child(tmp_path, _QT_APP + """
    def test_slot_raises():
        def slot():
            raise ValueError("boom in slot")
        QTimer.singleShot(0, slot)
        spin()

    def test_next_is_clean():
        spin()
    """)
    assert result.returncode == 1, outcome(result)
    assert "1 failed, 1 passed" in result.stdout, outcome(result)
    assert "boom in slot" in result.stdout, outcome(result)


def test_pytest_fail_inside_qrunnable_is_reported_not_fatal(tmp_path):
    # pytest.fail raises Failed, a BaseException: the exact case that
    # aborted the process from a pool job (HANDOFF 131).
    result = run_child(tmp_path, _QT_APP + """
    class Job(QRunnable):
        def run(self):
            pytest.fail("fake called from a pool job")

    def test_pool_job_fails():
        job = Job()
        QThreadPool.globalInstance().start(job)
        QThreadPool.globalInstance().waitForDone(5000)

    def test_after():
        pass
    """)
    assert result.returncode == 1, outcome(result)
    assert "1 failed, 1 passed" in result.stdout, outcome(result)
    assert "fake called from a pool job" in result.stdout, outcome(result)


def test_exception_in_qthread_run_and_python_thread_fail_the_test(tmp_path):
    result = run_child(tmp_path, _QT_APP + """
    import threading

    class Worker(QThread):
        def run(self):
            raise RuntimeError("boom in QThread.run")

    def test_qthread():
        worker = Worker()
        worker.start()
        worker.wait(5000)

    def test_python_thread():
        thread = threading.Thread(target=lambda: pytest.fail("boom in threading"))
        thread.start()
        thread.join()
    """)
    assert result.returncode == 1, outcome(result)
    assert "2 failed" in result.stdout, outcome(result)
    assert "boom in QThread.run" in result.stdout, outcome(result)
    assert "boom in threading" in result.stdout, outcome(result)


def test_clean_qt_test_passes_under_the_harness(tmp_path):
    """Negative case: the harness does not fail ordinary Qt work."""
    result = run_child(tmp_path, _QT_APP + """
    class Worker(QThread):
        def run(self):
            self.msleep(20)

    def test_worker_joined_by_the_test():
        worker = Worker()
        worker.start()
        worker.wait(5000)
        QTimer.singleShot(0, lambda: None)
        spin()
    """)
    assert result.returncode == 0, outcome(result)
    assert "1 passed" in result.stdout, outcome(result)


def test_qthread_left_running_fails_the_test_and_is_listed(tmp_path, monkeypatch):
    result = run_child(tmp_path, _QT_APP + """
    import kicadgen_test_harness
    kicadgen_test_harness.THREAD_JOIN_TIMEOUT_MS = 200

    class Sleeper(QThread):
        def run(self):
            self.msleep(1500)

    KEEP = []

    def test_leaks_a_thread():
        worker = Sleeper()
        KEEP.append(worker)
        worker.start()
    """)
    assert result.returncode == 1, outcome(result)
    assert "still running" in result.stdout, outcome(result)
    assert "QThreads still running at teardown" in result.stdout, outcome(result)
    assert "Sleeper" in result.stdout, outcome(result)


_NETWORK_CHILD = """
    import socket
    import pytest
    import kicadgen_test_harness
    kicadgen_test_harness.ENFORCE_NO_NETWORK = {enforce}

    def test_swallowed_connect():
        try:
            socket.create_connection(("203.0.113.1", 80), timeout=1)
        except OSError:
            pass  # the code under test goes offline; the harness still reports

    @pytest.mark.allow_network
    def test_marked_is_not_guarded():
        assert socket.socket.connect.__name__ != "method"
"""


def test_network_connection_is_refused_and_fails_the_test_when_enforced(tmp_path):
    result = run_child(tmp_path, _NETWORK_CHILD.format(enforce=True))
    assert result.returncode == 1, outcome(result)
    # The guard reports at teardown: the attempt was swallowed by the body.
    assert "2 passed, 1 error" in result.stdout, outcome(result)
    assert "ERROR test_child.py::test_swallowed_connect" in result.stdout, outcome(result)
    assert "203.0.113.1" in result.stdout, outcome(result)


def test_network_connection_is_refused_and_listed_in_report_mode(tmp_path):
    result = run_child(tmp_path, _NETWORK_CHILD.format(enforce=False))
    assert result.returncode == 0, outcome(result)
    assert "Network access blocked" in result.stdout, outcome(result)
    assert "test_swallowed_connect" in result.stdout, outcome(result)
    assert "203.0.113.1" in result.stdout, outcome(result)


def test_hook_chained_from_a_test_hook_is_not_charged_as_escaped(monkeypatch):
    """A hook a test installs on purpose owns the exception (launcher, crash reports)."""
    seen = []

    def test_hook(*exc_info):
        seen.append(exc_info[0])
        harness._record_escaped_exception(*exc_info)

    monkeypatch.setattr(sys, "excepthook", test_hook)
    try:
        raise RuntimeError("deliberate")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert seen == [RuntimeError]
    assert harness._ESCAPED == []


def test_network_guard_in_process(request):
    with pytest.raises(harness.NetworkAccessBlocked):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with pytest.raises(OSError):
        urllib.request.urlopen("http://example.invalid/", timeout=1)
    attempts = request.node.network_attempts
    assert len(attempts) == 2
    assert "192.0.2.1" in attempts[0] and "example.invalid" in attempts[1]
    # These attempts would fail this test at teardown: that part is proved
    # by the child-process test above.
    attempts.clear()


def test_loopback_is_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()
    first, second = socket.socketpair()
    first.close()
    second.close()


def test_data_dir_is_inside_the_test_tmp_folder(tmp_path):
    from kicadgen import app_paths
    data_dir = Path(os.environ["KICADGEN_DATA_DIR"])
    assert data_dir.is_relative_to(tmp_path)
    assert app_paths.user_data_dir() == data_dir
    assert app_paths.logs_dir().is_relative_to(tmp_path)
    assert app_paths.default_projects_dir().is_relative_to(tmp_path)


def test_shipped_profile_database_still_seeds_the_tmp_data_dir(tmp_path, monkeypatch):
    from kicadgen import app_paths
    shipped = app_paths.resource_path("data", "analysis_profiles.sqlite3")
    if not shipped.is_file():
        pytest.skip("no shipped profile database in this checkout")
    monkeypatch.delenv("KICADGEN_PROFILE_DB")
    seeded = app_paths.data_file("analysis_profiles.sqlite3", seed=True)
    assert seeded.is_relative_to(tmp_path)
    assert seeded.read_bytes() == shipped.read_bytes()


def test_hang_report_receives_qt_critical_messages():
    from PyQt6.QtCore import qCritical
    qCritical("harness-probe critical message")
    text = harness.HANG_REPORT.read_text(encoding="utf-8", errors="replace")
    assert "harness-probe critical message" in text
    assert "test_hang_report_receives_qt_critical_messages" in text


def test_conftest_compiles_without_syntax_warnings():
    import warnings
    source = (TESTS / "conftest.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        compile(source, "conftest.py", "exec")


#: Keeps the QApplication alive between tests: a QApplication owned by a local
#: variable is destroyed at the end of the test, which invalidates the global
#: thread pool and the widgets other tests still hold (see test_photo_inventory_gui).
_APP = None


def _application():
    """Return the process QApplication, creating and pinning it if needed."""
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_shutdown_main_window_joins_workers_and_destroys_the_window():
    """The shared `window` fixture destroys its window inside the owning test."""
    pytest.importorskip("pyvistaqt")
    from PyQt6 import sip
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication, QMainWindow
    from qt_fixtures import shutdown_main_window
    app = _application()

    class Worker(QThread):
        def run(self):
            self.msleep(150)

    class FakeWindow(QMainWindow):
        pass

    window = FakeWindow()
    window._worker = Worker()
    window._abandoned_workers = [Worker()]
    for worker in (window._worker, *window._abandoned_workers):
        worker.start()
    workers = [window._worker, *window._abandoned_workers]
    shutdown_main_window(window, app)
    assert all(not worker.isRunning() for worker in workers)
    assert sip.isdeleted(window)


def test_shutdown_main_window_keeps_a_window_whose_worker_does_not_stop(monkeypatch):
    """Negative case: a worker still running never gets its window destroyed."""
    pytest.importorskip("pyvistaqt")
    from PyQt6 import sip
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication, QMainWindow
    from qt_fixtures import shutdown_main_window
    app = _application()
    monkeypatch.setattr(harness, "THREAD_JOIN_TIMEOUT_MS", 50)

    class Worker(QThread):
        def run(self):
            self.msleep(600)

    window = QMainWindow()
    window._worker = Worker()
    window._worker.start()
    leaks_before = len(harness._THREAD_LEAKS)
    shutdown_main_window(window, app)
    try:
        assert not sip.isdeleted(window)
        assert window in harness._GRAVEYARD
        assert len(harness._THREAD_LEAKS) == leaks_before + 1
    finally:
        window._worker.wait(5000)
        harness._GRAVEYARD.remove(window)
        harness._GRAVEYARD.remove(window._worker)
        del harness._THREAD_LEAKS[leaks_before:]
