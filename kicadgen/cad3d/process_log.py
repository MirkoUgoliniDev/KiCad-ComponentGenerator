"""@brief Stream CAD subprocess output to a durable, readable live log."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from pathlib import Path
from ..services.messages import render_child_line, tx


#: Quanti battiti devono entrare in una finestra di silenzio.
#:
#: Il watchdog qui sotto uccide il figlio dopo `timeout` secondi senza output.
#: Perche' quel criterio resti onesto, ogni passo atomico lungo del figlio deve
#: battere piu' in fretta di cosi'; l'intervallo NON va scelto a parte, o le due
#: costanti divergono in silenzio e si torna al difetto di partenza (un import
#: sano ucciso a 300 s esatti). Con 5 battiti per finestra servono CINQUE
#: battute perse di fila -- non una -- perche' un lavoro vivo venga scambiato
#: per fermo: e' il margine che paga lo scheduler del sistema operativo, una
#: pipe congestionata e il costo di formattare la riga. Alzare il numero
#: stringe il margine di errore sul singolo battito; abbassarlo riempie il log
#: di righe. Non e' tarato su un componente osservato: e' un rapporto, quindi
#: vale identico se il timeout cambia.
HEARTBEAT_BEATS_PER_TIMEOUT = 5

#: Ogni quanto si guarda il canale di rinuncia mentre il figlio lavora.
#:
#: NON e' una soglia tarata su un caso osservato: e' il ritardo massimo fra il
#: clic su «Interrompi» e la morte del processo figlio, cioe' una promessa
#: fatta a chi guarda. Il canale non si puo' guardare solo dove agisce il
#: watchdog del silenzio -- quello scatta dopo `timeout` secondi di silenzio
#: (300 s in questo programma), quindi su un figlio muto l'interruzione
#: arriverebbe cinque minuti dopo la richiesta -- ne' solo a ogni riga letta,
#: perche' un figlio che tace non ne produce nessuna. Un decimo di secondo e'
#: sotto la soglia in cui una persona percepisce un ritardo e costa dieci
#: risvegli al secondo di un thread che dorme: alzarlo rende l'interruzione
#: percepibile, abbassarlo non la rende piu' utile.
CANCEL_POLL_INTERVAL_S = 0.1


class ProcessCancelled(Exception):
    """@brief Raised when the child was killed because the caller asked to stop.

    @note Distinta da `subprocess.TimeoutExpired` DI PROPOSITO: il watchdog
        uccide un lavoro che sembra fermo (ed e' una diagnosi, da scrivere nel
        log come tale), mentre questa dice che e' stata una persona a fermare
        un lavoro sano. Confonderle vorrebbe dire scrivere «nessun output per
        300 s» sotto un pulsante appena premuto, cioe' accusare il programma di
        un guasto che non c'e' stato.
    """

    def __init__(self, command, output=""):
        """@brief Carry the command and the output collected before the kill.

        @param command Command line whose child was killed.
        @param output Text the child had already written, kept for the log.
        """
        super().__init__(tx("Esecuzione interrotta su richiesta."))
        self.cmd = command
        self.output = output


def heartbeat_interval(timeout) -> float:
    """@brief Derive the child's heartbeat period from this watchdog's own timeout.

    @param timeout Seconds of silence after which `run_logged` kills the child.
    @return Seconds between two heartbeat lines, strictly smaller than @p timeout.
    @throws ValueError When @p timeout is not a positive finite number.
    @note Questa funzione e' l'unico punto in cui il legame fra i due numeri
        esiste. Chi lancia il processo figlio deve passargli QUESTO valore
        (per KiCadGen: il campo `heartbeat_interval_s` del file JSON dei
        parametri), mai riscrivere a mano lo stesso numero nello script del
        figlio: due copie della stessa costante si scollegano alla prima
        modifica e il watchdog torna a uccidere lavori sani.
    """
    seconds = float(timeout)
    if not (seconds > 0) or seconds in (float("inf"),):
        raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")
    return seconds / HEARTBEAT_BEATS_PER_TIMEOUT


def run_logged(command, log_path: Path, progress=None, timeout=120, creationflags=0,
               cancel=None):
    """@brief Execute CAD, flushing each output line before notifying the GUI.

    @param log_path UTF-8 append-only diagnostic file for the component.
    @param timeout Seconds of SILENCE after which the child is considered hung.
        Il conto riparte a ogni riga di output, quindi non e' un budget per
        l'intero lavoro.
    @param cancel Canale di rinuncia: un oggetto con `is_set()` e
        `wait(seconds)` (tipicamente `threading.Event`) che il chiamante mette
        a uno per chiedere la fine del lavoro. None = nessuno puo' fermarlo.
    @return CompletedProcess with merged stdout/stderr.
    @throws subprocess.TimeoutExpired After killing and reaping a hung child.
    @throws ProcessCancelled After killing the child because @p cancel was set.
    @note La rinuncia usa la STESSA macchina del watchdog -- l'unico posto in
        cui questo modulo uccide il figlio -- perche' le due cose sono la
        stessa azione con due motivi diversi; quel che cambia e' cio' che
        viene scritto nel log e l'eccezione sollevata. Un figlio ucciso su
        richiesta NON porta la riga «nessun output per N s»: non e' stato
        trovato fermo, e' stato fermato.
    @note Prima era un limite sulla durata complessiva, e su un modulo reale ha
        fatto perdere tutto: l'import del primo modello 3D (uno STEP da 8.8 MB)
        si e' mangiato i 300 secondi da solo, e il processo e' stato ucciso
        prima del secondo componente dei cinquanta. Un limite unico non
        distingue le due cose che contano -- un lavoro lento e un lavoro
        fermo -- e punisce proprio i lavori grandi, che sono quelli in cui
        rifare tutto costa di piu'.
    @note Il silenzio e' il criterio giusto SOLO finche' la premessa regge:
        che il figlio annunci quello che sta facendo. Su una corsa reale la
        premessa era falsa per un passo solo -- l'import di un modello di
        libreria, che parsa uno STEP grande senza stampare nulla -- e quel
        buco ha fatto uccidere un processo sano a 300 s esatti, al tredicesimo
        componente su cinquantadue. La correzione NON e' stata alzare il
        timeout (un numero piu' grande sarebbe solo la stessa soglia tarata sul
        caso visto, e ritarderebbe la diagnosi di ogni lavoro davvero fermo):
        e' stato far parlare il passo muto. Vedi `heartbeat_interval` e
        `freecad_parts._ImportHeartbeat`: ora ogni passo lungo batte, quindi
        il silenzio torna a significare "fermo".
    @note Non c'e' un tetto assoluto: un lavoro che avanza non viene
        interrotto per aver avanzato a lungo.
    """
    lines = []
    timed_out = threading.Event()
    cancelled = threading.Event()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] FreeCAD start\n")
        handle.flush()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              creationflags=creationflags, bufsize=1) as process:
            def kill_child():
                """Unico punto in cui il figlio viene ucciso: watchdog e rinuncia."""
                try:
                    process.kill()
                except OSError:
                    pass

            def expire():
                timed_out.set()
                kill_child()

            # Il thread di sorveglianza esiste solo se qualcuno puo' davvero
            # chiedere di fermarsi, e muore con la lettura: e' legato alla
            # vita di questa chiamata, non del programma.
            reading_over = threading.Event()

            def watch_cancel():
                while not reading_over.is_set():
                    if cancel.wait(CANCEL_POLL_INTERVAL_S):
                        cancelled.set()
                        kill_child()
                        return

            watcher = None
            if cancel is not None:
                if cancel.is_set():
                    # Rinuncia arrivata PRIMA di leggere la prima riga (il
                    # chiamante era gia' partito): si uccide subito invece di
                    # aspettare il primo giro del sorvegliante.
                    cancelled.set()
                    kill_child()
                else:
                    watcher = threading.Thread(target=watch_cancel, daemon=True)
                    watcher.start()
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
                        # Gli avvisi del figlio arrivano come modello + valori:
                        # si traducono qui (vedi messages.render_child_line).
                        progress("FreeCAD: " + render_child_line(line.rstrip()))
                    if timed_out.is_set() or cancelled.is_set():
                        break
                    timer = threading.Timer(timeout, expire)
                    timer.daemon = True
                    timer.start()
                process.wait()
            finally:
                timer.cancel()
                # Sveglia il sorvegliante anche quando la lettura finisce da
                # sola: senza, resterebbe a dormire su un processo gia' morto.
                reading_over.set()
                if watcher is not None:
                    watcher.join(timeout=2 * CANCEL_POLL_INTERVAL_S)
            output = "".join(lines)
            handle.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                         f"FreeCAD exit={process.returncode}, timeout={timed_out.is_set()}"
                         f", cancelled={cancelled.is_set()}"
                         # La diagnosi del watchdog si scrive solo se e' LEI ad
                         # aver ucciso il figlio. Quando la rinuncia e' gia'
                         # arrivata, il silenzio che segue e' l'effetto
                         # dell'uccisione, non la sua causa: annunciarlo
                         # accuserebbe di un blocco un lavoro che stava bene.
                         + (tx(" (nessun output per {timeout} s)", timeout=timeout)
                            if timed_out.is_set() and not cancelled.is_set() else "")
                         + (tx(" (interrotto su richiesta dell'utente)")
                            if cancelled.is_set() else "")
                         + "\n")
            handle.flush()
            # La rinuncia viene PRIMA del watchdog: se il silenzio e' scattato
            # subito dopo la richiesta, cio' che e' successo davvero e' che
            # qualcuno ha premuto il pulsante, e il log non deve raccontare un
            # blocco che non c'e' stato.
            if cancelled.is_set():
                raise ProcessCancelled(command, output=output)
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(command, timeout, output=output)
            return subprocess.CompletedProcess(command, process.returncode, output, "")
