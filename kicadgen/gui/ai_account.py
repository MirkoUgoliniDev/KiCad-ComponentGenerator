"""@brief One line under «Extract with AI»: OpenRouter balance and project cost.

The balance comes from OpenRouter's two free status endpoints (no tokens, no
model call), read in the background. The project cost is the running total
of `services.project_costs`, updated on every paid call as it happens: the
worker thread reports the call through a Qt signal, and the file is written
from the GUI thread.

The project a cost belongs to is the project of the RUN that paid it, not the
project in use when the queued signal arrives: `bind_run_project` captures it
when a run starts, and the worker-thread listener stamps every record with it
at the moment of the call. Without a bound run the project in use is taken.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QLabel

from ..services import openrouter
from ..services.messages import tx
from ..services.project_costs import ProjectCostsError, add_call, load_project_costs


def format_usd(amount: float) -> str:
    """@brief Money as the line shows it.

    @param amount USD.
    @return Two decimals from 1 USD up; four below, where two would round a
        real expense of a few tenths of a cent down to «0.00»; «< 0.0001»
        for a real but smaller amount, which four decimals would show as 0.
    """
    if 0 < amount < 0.00005:
        return "< 0.0001"
    return f"{amount:.2f}" if abs(amount) >= 1 else f"{amount:.4f}"


def _local_date(stamp: str) -> str:
    """Date of an ISO timestamp in the local time zone (the file stores UTC)."""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return stamp[:10]


class _UsageBridge(QObject):
    """Carries a paid call from the worker thread to the GUI thread."""

    recorded = pyqtSignal(dict)


class _BalanceSignals(QObject):
    ready = pyqtSignal(int, object, str)


class _BalanceTask(QRunnable):
    """Reads the balance off the GUI thread: it is a network request."""

    def __init__(self, token: int):
        super().__init__()
        self.token = token
        self.signals = _BalanceSignals()

    def run(self):
        """@brief Query OpenRouter and report the status or the error text."""
        try:
            status = openrouter.credit_status(force_refresh=True)
        except Exception as exc:  # noqa: BLE001 - riportato sulla riga, mai fatale
            self.signals.ready.emit(self.token, None, str(exc))
            return
        self.signals.ready.emit(self.token, status, "")


class AiAccountLine(QLabel):
    """@brief Label with the OpenRouter balance and the cost of the current project.

    @note The balance is read only when `balance_enabled` is true: the
        application turns it on in `main()`, so tests and offline runs never
        touch the network. Without an API key it says «not available».
    """

    def __init__(self, project_dir: Callable[[], Optional[object]],
                 log: Callable[[str], None], parent=None):
        """@brief Build the line and start listening to paid calls.

        @param project_dir Returns the current project folder, or None when
            the project has no name yet.
        @param log Writes one line into the AI log.
        @param parent Parent widget.
        """
        super().__init__(parent)
        self.setObjectName("ai_account")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setProperty("i18n_skip", True)
        self.setStyleSheet("color: #a6adc8;")
        self.balance_enabled = False
        self._project_dir = project_dir
        self._log = log
        self._status = None
        self._balance_error = ""
        self._balance_state = "idle"
        self._balance_time = ""
        self._token = 0
        self._tasks = {}
        self._reported_errors = set()
        #: Paid calls not written yet (file busy): (folder, record), written
        #: before the next call. Kept in memory only for this session.
        self._pending = []
        #: Project of the running analysis, captured by bind_run_project and
        #: read from worker threads: guarded by a lock.
        self._run_lock = threading.Lock()
        self._run_bound = False
        self._run_project: Optional[Path] = None
        self._bridge = _UsageBridge(self)
        self._bridge.recorded.connect(self._on_paid_call)
        self._listener = self._stamp_and_forward
        openrouter.set_usage_listener(self._listener)
        self.refresh()

    def bind_run_project(self, folder) -> None:
        """@brief Charge every paid call from now on to `folder`, the project of the run.

        @param folder Project folder captured when the run starts, or None when
            the project has no name yet (its calls are then logged as not
            recorded, one line each).
        @note Call it on the GUI thread right before starting a paid run and
            `release_run_project` when the run has finished. The folder is
            attached to each record in the worker thread at the time of the
            call, so a project switched while queued signals are still in
            flight cannot receive another project's costs.
        """
        with self._run_lock:
            self._run_bound = True
            self._run_project = Path(folder) if folder is not None else None

    def release_run_project(self) -> None:
        """@brief Stop charging calls to the bound run; later calls go to the project in use.

        @note Records already stamped keep their run project even when their
            queued signal is delivered after this call.
        """
        with self._run_lock:
            self._run_bound = False
            self._run_project = None

    def _stamp_and_forward(self, record: dict) -> None:
        """Worker-thread listener: stamp the bound run project, then hop to the GUI thread."""
        with self._run_lock:
            if self._run_bound:
                # A str, not a Path: the record crosses a queued Qt signal.
                record = dict(record, _run_bound=True,
                              _run_project=(str(self._run_project)
                                            if self._run_project is not None else None))
        self._bridge.recorded.emit(record)

    def detach(self) -> None:
        """@brief Stop listening, if this line is still the listener."""
        if openrouter._usage_listener == self._listener:
            openrouter.set_usage_listener(None)

    def refresh_balance(self) -> None:
        """@brief Read the balance again in the background (free, no tokens)."""
        if not self.balance_enabled:
            return
        try:
            openrouter.api_key()
        except Exception as exc:  # noqa: BLE001 - chiave assente: si dice e basta
            self._balance_state, self._balance_error = "error", str(exc)
            self.refresh()
            return
        self._token += 1
        task = _BalanceTask(self._token)
        task.setAutoDelete(False)
        task.signals.ready.connect(self._balance_ready)
        self._tasks[self._token] = task
        if self._balance_state != "ok":
            self._balance_state = "loading"
        self.refresh()
        QThreadPool.globalInstance().start(task)

    def _balance_ready(self, token, status, error) -> None:
        self._tasks.pop(token, None)
        if token != self._token:
            return  # superata da una lettura piu' recente
        if status is None:
            self._balance_state, self._balance_error = "error", error
        else:
            self._status, self._balance_state = status, "ok"
            self._balance_time = datetime.now().strftime("%H:%M")
        self.refresh()

    def _on_paid_call(self, record: dict) -> None:
        """Write the paid call into the project in use; never let an error escape.

        An exception leaving a Qt slot aborts the program in PyQt6: here it
        would happen in the middle of a paid analysis. Every failure becomes
        one log line. A file busy for a moment keeps the call in memory and
        writes it before the next one.
        """
        try:
            record = dict(record)
            if record.pop("_run_bound", False):
                folder = record.pop("_run_project", None)
            else:
                record.pop("_run_project", None)
                folder = self._project_dir()
            if folder is None:
                # Every dropped cost gets its own line: a de-duplicated
                # message would hide how much went unrecorded.
                self._log(tx("Costo di una chiamata AI non registrato ({cost} USD, {model}): "
                             "il progetto non ha un nome.", cost=self._cost_text(record),
                             model=record.get("model") or "?"))
                return
            self._pending.append((Path(folder), record))
            self._flush_pending()
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - uno slot che solleva chiude l'app
            self._log(tx("Costo di una chiamata AI non registrato nel progetto ({cost} USD): "
                         "{exc}", cost=self._cost_text(record), exc=exc))

    @staticmethod
    def _cost_text(record) -> str:
        """Cost of a record for a log line; «?» when it was not reported."""
        cost = record.get("cost_usd") if isinstance(record, dict) else None
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return "?"
        return format_usd(float(cost))

    def _flush_pending(self) -> None:
        """Write the queued calls in order; stop at the first busy file."""
        while self._pending:
            folder, record = self._pending[0]
            try:
                add_call(folder, record)
            except ProjectCostsError as exc:
                # File damaged: never overwritten, so the call cannot go in.
                # One line per dropped call, never de-duplicated.
                self._pending.pop(0)
                self._log(tx("Costo di una chiamata AI non registrato nel progetto ({cost} USD): "
                             "{exc}", cost=self._cost_text(record), exc=exc))
                continue
            except OSError as exc:
                self._report(tx("File dei costi occupato ({exc}): {count} chiamate in attesa, "
                                "saranno scritte alla prossima.", exc=exc,
                                count=len(self._pending)))
                return
            self._pending.pop(0)

    def _report(self, message: str) -> None:
        """One log line per distinct problem, not one per call."""
        if message not in self._reported_errors:
            self._reported_errors.add(message)
            self._log(message)

    def refresh(self) -> None:
        """@brief Recompose text and tooltip from the balance and the project file."""
        parts, details = [], []
        remaining = self._status.remaining if self._status is not None else None
        if self._balance_state == "ok" and remaining is not None:
            parts.append(tx("Saldo OpenRouter: {amount} USD", amount=format_usd(remaining)))
            details.append(tx("Saldo: credito residuo ({limit}), letto alle {time}.",
                              limit=self._status.limiting_factor, time=self._balance_time))
        elif self._balance_state == "ok" and self._status.account_error:
            # Saldo dell'account non letto e chiave senza tetto: mancano i
            # dati, non c'e' «nessun limite».
            parts.append(tx("Saldo OpenRouter: non disponibile"))
            details.append(tx("Saldo non letto: {error}", error=self._status.account_error))
        elif self._balance_state == "ok":
            parts.append(tx("Saldo OpenRouter: nessun limite"))
            details.append(tx("Saldo: l'API non riporta un limite di credito."))
        elif self._balance_state == "loading":
            parts.append(tx("Saldo OpenRouter: lettura…"))
        elif self._balance_state == "error":
            parts.append(tx("Saldo OpenRouter: non disponibile"))
            details.append(tx("Saldo non letto: {error}", error=self._balance_error))
        folder = self._project_dir()
        if folder is not None:
            try:
                costs = load_project_costs(folder)
            except OSError:
                parts.append(tx("Costo del progetto: lettura…"))
            except ProjectCostsError as exc:
                parts.append(tx("Costo del progetto: non leggibile"))
                details.append(tx("File dei costi non leggibile, lasciato com’è: {exc}",
                                  exc=exc))
            else:
                if not costs.calls:
                    parts.append(tx("Costo del progetto: nessuna chiamata a pagamento"))
                elif costs.complete:
                    parts.append(tx("Costo del progetto: {amount} USD",
                                    amount=format_usd(costs.total_usd)))
                else:
                    parts.append(tx("Costo del progetto: almeno {amount} USD",
                                    amount=format_usd(costs.total_usd)))
                since = _local_date(costs.since)
                if costs.calls == 1:
                    details.append(tx("Costo del progetto: 1 chiamata a pagamento dal "
                                      "{since}, tutti i passaggi compresi.", since=since))
                elif costs.calls:
                    details.append(tx("Costo del progetto: {calls} chiamate a pagamento dal "
                                      "{since}, tutti i passaggi compresi.",
                                      calls=costs.calls, since=since))
                if not costs.complete:
                    details.append(tx("{missing} chiamate senza costo riportato: il totale "
                                      "è sottostimato.", missing=costs.calls_without_cost))
        details.append(tx("Le risposte servite dalla cache locale non costano e non "
                          "sono contate."))
        # Riga di un modulo: resta al suo posto anche senza dati.
        self.setText("  ·  ".join(parts) or "—")
        self.setToolTip(tx("Credito e spesa AI") + "\n" + "\n".join(details))
