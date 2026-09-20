"""@brief Stream CAD subprocess output to a durable, readable live log."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from pathlib import Path


def run_logged(command, log_path: Path, progress=None, timeout=120, creationflags=0):
    """@brief Execute CAD, flushing each output line before notifying the GUI.

    @param log_path UTF-8 append-only diagnostic file for the component.
    @param timeout Seconds of SILENCE after which the child is considered hung.
        Il conto riparte a ogni riga di output, quindi non e' un budget per
        l'intero lavoro.
    @return CompletedProcess with merged stdout/stderr.
    @throws subprocess.TimeoutExpired After killing and reaping a hung child.
    @note Prima era un limite sulla durata complessiva, e su un modulo reale ha
        fatto perdere tutto: l'import del primo modello 3D (uno STEP da 8.8 MB)
        si e' mangiato i 300 secondi da solo, e il processo e' stato ucciso
        prima del secondo componente dei cinquanta. Un limite unico non
        distingue le due cose che contano -- un lavoro lento e un lavoro
        fermo -- e punisce proprio i lavori grandi, che sono quelli in cui
        rifare tutto costa di piu'.
    @note Il silenzio e' il criterio giusto perche' questo processo annuncia
        ogni componente e ogni import mentre procede: finche' parla, sta
        lavorando. Non c'e' un tetto assoluto: un lavoro che avanza non viene
        interrotto per aver avanzato a lungo.
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
                    # Ogni riga e' una prova che il processo sta lavorando: il
                    # conto del silenzio riparte da zero.
                    timer.cancel()
                    lines.append(line)
                    handle.write(line)
                    handle.flush()
                    if progress and line.strip():
                        progress("FreeCAD: " + line.rstrip())
                    if timed_out.is_set():
                        break
                    timer = threading.Timer(timeout, expire)
                    timer.daemon = True
                    timer.start()
                process.wait()
            finally:
                timer.cancel()
            output = "".join(lines)
            handle.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                         f"FreeCAD exit={process.returncode}, timeout={timed_out.is_set()}"
                         + (f" (nessun output per {timeout} s)" if timed_out.is_set() else "")
                         + "\n")
            handle.flush()
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(command, timeout, output=output)
            return subprocess.CompletedProcess(command, process.returncode, output, "")
