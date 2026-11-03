"""@brief The candidate preview's FreeCAD child: one launch with or without a cancel channel (2026-11-03).

Without a channel the preview used a bare `subprocess.run`: its timeout
killed only the launcher, not the tree, and the child had no
PYTHONDONTWRITEBYTECODE, so it left __pycache__ in the installation folder.
No FreeCAD: plain Python children stand in for it.
"""
import subprocess
import sys
import time

import pytest

from kicadgen.gui import model_candidates_dialog as mcd
from tests.test_run_logged_orphans import _alive, _wait_pid

#: Child leaving a sleeping grandchild (pid written to argv[1]) and then
#: staying alive itself, or exiting when argv[2] == "exit".
_TREE = r"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
    "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
    sys.argv[1]], stdout=sys.stdout)
print("KICADGEN_OK", flush=True)
if sys.argv[2] != "exit":
    time.sleep(60)
"""


@pytest.mark.parametrize("cancel", [None, "event"])
def test_the_child_never_writes_bytecode(cancel):
    """@brief Positive, both paths: the child sees PYTHONDONTWRITEBYTECODE=1."""
    import threading
    channel = threading.Event() if cancel else None
    done = mcd._run_preview_child(
        [sys.executable, "-c", "import os; print(os.environ.get('PYTHONDONTWRITEBYTECODE'))"],
        channel)
    assert done.stdout.strip() == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="tree kill through the Windows job")
def test_the_whole_run_limit_kills_the_tree_without_a_channel(monkeypatch, tmp_path):
    """@brief Positive: on expiry the grandchild dies too, not only the launcher."""
    monkeypatch.setattr(mcd, "PREVIEW_TIMEOUT_S", 1.5)
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        mcd._run_preview_child([sys.executable, "-c", _TREE, str(pid_file), "stay"])
    assert time.monotonic() - started < 15
    grandchild = _wait_pid(pid_file)
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "the grandchild survived the preview's timeout"


def test_an_exited_child_with_an_orphan_on_its_pipe_returns_its_output(monkeypatch, tmp_path):
    """@brief Negative: a child that finished is a success, not a timeout, despite the orphan."""
    monkeypatch.setattr(mcd, "PREVIEW_TIMEOUT_S", 20)
    started = time.monotonic()
    done = mcd._run_preview_child(
        [sys.executable, "-c", _TREE, str(tmp_path / "grandchild.pid"), "exit"])
    assert "KICADGEN_OK" in done.stdout
    assert time.monotonic() - started < 15


def test_no_bare_subprocess_run_is_left():
    """@brief Structural: the preview launch never goes through subprocess.run."""
    import inspect
    assert "subprocess.run(" not in inspect.getsource(mcd._run_preview_child)
