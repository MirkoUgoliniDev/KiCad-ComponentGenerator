"""@brief A descendant that outlives the FreeCAD child no longer holds the run (2026-11-03).

The child started a grandchild that inherited the output pipe and exited.
`run_logged` read the pipe until EOF, which never came while the grandchild
lived: the watchdog killed nothing (the child was gone), a cancel did
nothing, and when the grandchild finally ended the run raised
`TimeoutExpired` for a child that had exited with its success marker, so
`_run_freecad` deleted a healthy STEP. No FreeCAD is needed: the child and
the grandchild are plain Python processes.
"""
import subprocess
import sys
import threading
import time

import pytest

from kicadgen.cad3d import process_log

#: Child that leaves a sleeping grandchild on its own stdout and exits at once
#: with the success marker. The grandchild writes its pid first, so the test
#: can check afterwards that it was ended.
_LEAVES_A_GRANDCHILD = r"""
import subprocess, sys
pid_file = sys.argv[1]
grandchild = subprocess.Popen(
    [sys.executable, "-c",
     "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)",
     pid_file], stdout=sys.stdout, stderr=sys.stdout)
print("KICADGEN_STEP_OK", flush=True)
"""

#: Child that prints its marker and exits, with no descendant at all.
_EXITS_ALONE = 'print("KICADGEN_STEP_OK", flush=True)\n'

#: Child that stays alive and silent: a genuinely stuck job.
_STAYS_SILENT = "import time\ntime.sleep(30)\n"


def _alive(pid: int) -> bool:
    """Whether a process with this pid is still running (Windows API, or os.kill elsewhere)."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)  # query + SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    import os
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_pid(pid_file, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(pid_file.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    pytest.fail("the grandchild never started")


def test_a_child_that_exits_leaving_a_grandchild_returns_promptly(tmp_path):
    """@brief Positive: the run ends with the child, with its exit code and output, no timeout."""
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    result = process_log.run_logged([sys.executable, "-c", _LEAVES_A_GRANDCHILD, str(pid_file)],
                                    tmp_path / "freecad.log", timeout=1)
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert "KICADGEN_STEP_OK" in result.stdout
    # Far below the grandchild's 30 s sleep: the run did not wait for it.
    assert elapsed < 15, f"run_logged waited {elapsed:.1f} s for an orphan"
    log = (tmp_path / "freecad.log").read_text(encoding="utf-8")
    assert "timeout=False" in log and "cancelled=False" in log
    if sys.platform == "win32":
        # The survivor belonged to the run's job: it was ended with it.
        grandchild = _wait_pid(pid_file)
        deadline = time.monotonic() + 10
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), "the grandchild survived the run"


def test_a_child_that_exits_alone_is_unaffected(tmp_path):
    """@brief Negative: a child with no descendant ends as before, no straggler note."""
    result = process_log.run_logged([sys.executable, "-c", _EXITS_ALONE],
                                    tmp_path / "freecad.log", timeout=5)
    assert result.returncode == 0 and "KICADGEN_STEP_OK" in result.stdout
    log = (tmp_path / "freecad.log").read_text(encoding="utf-8")
    assert "timeout=False" in log
    assert "discendenti" not in log


def test_a_live_silent_child_is_still_a_timeout(tmp_path):
    """@brief Negative: a child that is ALIVE and silent is still killed by the watchdog."""
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        process_log.run_logged([sys.executable, "-c", _STAYS_SILENT],
                               tmp_path / "freecad.log", timeout=1)
    assert time.monotonic() - started < 15


def test_cancel_reaches_a_child_whose_grandchild_holds_the_pipe(tmp_path):
    """@brief Positive: the give-up stops a run even while a descendant holds the pipe."""
    script = _LEAVES_A_GRANDCHILD.replace('print("KICADGEN_STEP_OK", flush=True)',
                                          "import time; time.sleep(30)")
    pid_file = tmp_path / "grandchild.pid"
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(process_log.ProcessCancelled):
        process_log.run_logged([sys.executable, "-c", script, str(pid_file)],
                               tmp_path / "freecad.log", timeout=60, cancel=cancel)
    assert time.monotonic() - started < 15
    if sys.platform == "win32":
        grandchild = _wait_pid(pid_file)
        deadline = time.monotonic() + 10
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild)


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects exist only on Windows")
def test_contain_process_puts_the_child_in_a_job(tmp_path):
    """@brief Positive: a real child gets a job, released idempotently."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_log.contain_process(process)
    finally:
        process_log.kill_process_tree(process)
        process.wait(timeout=20)
        process_log.release_process(process)
        process_log.release_process(process)
    assert process.returncode != 0


def test_contain_process_leaves_doubles_and_other_platforms_alone():
    """@brief Negative: no handle or another platform -> no job, nothing raised."""
    class Double:
        pid = -1
    assert process_log.contain_process(Double()) is None
    assert process_log.contain_process(Double(), platform="linux") is None
    process_log.release_process(Double())


def test_select_descendants_refuses_children_born_after_the_root_exited():
    """@brief Negative: once the root is gone, children of a pid re-user are never selected."""
    stamps = {10: 100, 11: 150, 12: 300, 13: 160}
    children = {10: [11, 12], 11: [13]}
    picked = process_log.select_descendants(10, children, stamps.get,
                                            root_born=100, root_exited=200)
    assert 12 not in picked
    assert set(picked) == {11, 13}
    # Without the exit bound (live root) every verified child is ours, as before.
    assert set(process_log.select_descendants(10, children, stamps.get)) == {11, 12, 13}
