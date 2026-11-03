"""@brief A killed FreeCAD child takes its own children with it (2026-10-17).

The suite failed with WinError 32: the measuring child was a launcher that
started the real interpreter as ITS child; `Popen.kill` ended the launcher
only, and the survivor kept the output file open.
"""
import subprocess
import sys
import time

import pytest

from kicadgen.cad3d.process_log import kill_process_tree

_PARENT = r"""
import subprocess, sys, time
held = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c",
    "import sys, time; f = open(sys.argv[1], 'a'); print('ready', flush=True); time.sleep(120)", held],
    stdout=sys.stdout)
time.sleep(120)
"""


def _spawn(tmp_path):
    held = tmp_path / "held.out"
    sink = (tmp_path / "parent.out").open("w")
    process = subprocess.Popen([sys.executable, "-c", _PARENT, str(held)], stdout=sink,
                               stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 20
    while "ready" not in (tmp_path / "parent.out").read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    return process, held, sink


@pytest.mark.skipif(sys.platform != "win32", reason="open files block deletion only on Windows")
def test_the_grandchild_dies_and_its_file_can_be_deleted(tmp_path):
    process, held, sink = _spawn(tmp_path)
    kill_process_tree(process)
    process.wait(timeout=20)
    sink.close()
    deadline = time.monotonic() + 10
    while True:
        try:
            held.unlink()
            break
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    assert not held.exists()


def test_an_exited_process_is_a_no_op(monkeypatch):
    """@brief Negative: killing a finished child with no descendant raises and ends nothing.

    CONTRACT CHANGED 2026-11-03 (review): an exited child used to skip the
    descendant search entirely, so a grandchild holding its pipe survived
    every kill. The search now runs through the handle Popen keeps, bounded
    by the child's exit time; what must hold is that a child that left no
    descendant makes nothing be terminated.
    """
    from kicadgen.cad3d import process_log
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=20)
    ended = []
    monkeypatch.setattr(process_log, "_windows_terminate", ended.append)
    kill_process_tree(process, platform="win32")
    assert ended == []


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects exist only on Windows")
def test_an_exited_contained_child_still_takes_its_orphan_with_it(tmp_path):
    """@brief Positive: the child exited, its grandchild lives; the job kill ends the grandchild.

    The orphan's chain to the child runs through processes that already
    exited (a venv launcher starts the real interpreter as its child), so
    the snapshot cannot link it any more: only the job can, which is why
    every FreeCAD launch is contained.
    """
    from kicadgen.cad3d import process_log
    from tests.test_run_logged_orphans import _alive
    pid_file = tmp_path / "orphan.pid"
    script = ("import subprocess, sys\n"
              "subprocess.Popen([sys.executable, '-c', \"import os, sys, time; "
              "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)\", sys.argv[1]])\n")
    process = subprocess.Popen([sys.executable, "-c", script, str(pid_file)])
    assert process_log.contain_process(process)
    try:
        process.wait(timeout=20)
        deadline = time.monotonic() + 20
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < deadline, "the orphan never started"
            time.sleep(0.05)
        orphan = int(pid_file.read_text())
        assert _alive(orphan)
        kill_process_tree(process, platform="win32")
        deadline = time.monotonic() + 10
        while _alive(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(orphan), "the orphan survived the kill of its exited parent"
    finally:
        process_log.release_process(process)
