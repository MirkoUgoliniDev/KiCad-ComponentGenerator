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
    """@brief Negative: killing a finished child raises nothing and looks for no descendants."""
    from kicadgen.cad3d import process_log
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=20)
    calls = []
    monkeypatch.setattr(process_log, "_windows_descendants", lambda pid: calls.append(pid) or [])
    kill_process_tree(process, platform="win32")
    assert calls == []
