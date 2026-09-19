"""@brief Stream CAD subprocess output to a durable, readable live log."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from pathlib import Path


def run_logged(command, log_path: Path, progress=None, timeout=120, creationflags=0):
    """@brief Execute CAD, flushing each output line before notifying the GUI.

    @param log_path UTF-8 append-only diagnostic file for the component.
    @param timeout Maximum process lifetime in seconds, including silent hangs.
    @return CompletedProcess with merged stdout/stderr.
    @throws subprocess.TimeoutExpired After killing and reaping a hung child.
    """
    lines = []
    timed_out = threading.Event()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] FreeCAD start\n")
        handle.flush()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              creationflags=creationflags, bufsize=1) as process:
            def expire():
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass
            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                for line in process.stdout:
                    lines.append(line)
                    handle.write(line)
                    handle.flush()
                    if progress and line.strip():
                        progress("FreeCAD: " + line.rstrip())
                process.wait()
            finally:
                timer.cancel()
            output = "".join(lines)
            handle.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                         f"FreeCAD exit={process.returncode}, timeout={timed_out.is_set()}\n")
            handle.flush()
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(command, timeout, output=output)
            return subprocess.CompletedProcess(command, process.returncode, output, "")
