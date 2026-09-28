"""@brief Saldo OpenRouter e costo totale del progetto (utente, 2026-09-24).

Il costo di OGNI chiamata pagata finisce in `ai-costs.json` nella cartella del
progetto, dal thread che l'ha fatta al file tramite la GUI; il saldo si legge
con le due interrogazioni gratuite di OpenRouter, mai nei test.
Nessuna chiamata di rete: `credit_status` e' sempre sostituito.
"""
import json
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from kicadgen.services import openrouter
from kicadgen.services.project_costs import (COSTS_FILE, ProjectCostsError, add_call,
                                             load_project_costs, start_run)


@pytest.fixture(autouse=True)
def no_listener():
    openrouter.set_usage_listener(None)
    yield
    openrouter.set_usage_listener(None)


def _wait(app, predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


# ── Servizio ────────────────────────────────────────────────────────────────

def test_a_project_without_calls_costs_nothing_and_writes_nothing(tmp_path):
    costs = load_project_costs(tmp_path)
    assert costs.calls == 0 and costs.total_usd == 0 and costs.complete
    assert not (tmp_path / COSTS_FILE).exists()


def test_calls_accumulate_in_the_project_file(tmp_path):
    add_call(tmp_path, {"cost_usd": 0.5, "model": "a/b", "prompt_tokens": 10,
                        "completion_tokens": 5})
    costs = add_call(tmp_path, {"cost_usd": 0.3954, "model": "a/b"})
    assert costs.calls == 2 and costs.total_usd == pytest.approx(0.8954)
    assert load_project_costs(tmp_path) == costs
    data = json.loads((tmp_path / COSTS_FILE).read_text(encoding="utf-8"))
    assert [e["cost_usd"] for e in data["entries"]] == [0.5, 0.3954]
    assert not list(tmp_path.glob(".ai-costs-*"))  # nessun temporaneo rimasto


@pytest.mark.parametrize("cost", [None, True, float("nan"), -1.0, "0.2"])
def test_a_call_without_a_usable_cost_is_counted_but_marks_the_total_partial(tmp_path, cost):
    """Negativo: un costo mancante o assurdo non entra nel totale, ma la chiamata si'."""
    add_call(tmp_path, {"cost_usd": 0.1})
    costs = add_call(tmp_path, {"cost_usd": cost})
    assert costs.calls == 2 and costs.total_usd == pytest.approx(0.1)
    assert costs.calls_without_cost == 1 and not costs.complete


@pytest.mark.parametrize("content", [
    "non json",
    json.dumps({"format": "altro"}),
    json.dumps({"format": "kicadgen-ai-costs", "version": 99, "total_usd": 0, "calls": 0,
                "calls_without_cost": 0, "entries": []}),
    json.dumps({"format": "kicadgen-ai-costs", "version": 1, "total_usd": True, "calls": 0,
                "calls_without_cost": 0, "entries": []}),
    json.dumps({"format": "kicadgen-ai-costs", "version": 1, "total_usd": 1, "calls": 1,
                "calls_without_cost": 2, "entries": []}),
])
def test_an_unusable_file_is_refused_and_never_overwritten(tmp_path, content):
    """Negativo: uno storico illeggibile resta com'e', la chiamata non lo cancella."""
    path = tmp_path / COSTS_FILE
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ProjectCostsError):
        load_project_costs(tmp_path)
    with pytest.raises(ProjectCostsError):
        add_call(tmp_path, {"cost_usd": 0.1})
    assert path.read_text(encoding="utf-8") == content


# ── Punto di intercettazione: ogni chiamata pagata ──────────────────────────

def test_every_paid_answer_reaches_the_listener_with_its_cost():
    seen = []
    openrouter.set_usage_listener(seen.append)
    openrouter._record_usage({"model": "x/y", "usage": {"cost": 0.25, "prompt_tokens": 7,
                                                        "completion_tokens": 3}})
    # Contract extended (audit 138, B10): every record carries the time of
    # its call, so a late write keeps the call's date and price hour.
    from datetime import datetime
    stamp = seen[0].pop("time")
    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert seen == [{"cost_usd": 0.25, "model": "x/y", "prompt_tokens": 7,
                     "completion_tokens": 3}]


def test_a_failing_listener_never_breaks_the_paid_call():
    """Negativo: l'errore dell'ascoltatore non risale; il consuntivo resta giusto."""
    def broken(record):
        raise RuntimeError("oggetto Qt distrutto")
    openrouter.reset_usage()
    openrouter.set_usage_listener(broken)
    openrouter._record_usage({"usage": {"cost": 0.1}})
    openrouter._record_usage("risposta non dizionario")
    assert openrouter.usage_totals().calls == 2


# ── Riga «Credito AI» nel riquadro «Strumenti AI» ───────────────────────────

def test_a_call_from_a_worker_thread_is_written_into_the_project(app, tmp_path):
    """Positivo, percorso vero: thread di lavoro -> segnale Qt -> file del progetto."""
    from kicadgen.gui.ai_account import AiAccountLine
    log = []
    line = AiAccountLine(lambda: tmp_path, log.append)
    # The line shows the current analysis (point 135): one is started first.
    line.start_analysis(tmp_path)
    thread = threading.Thread(target=openrouter._record_usage,
                              args=({"model": "m", "usage": {"cost": 1.5}},))
    thread.start()
    thread.join()
    _wait(app, lambda: load_project_costs(tmp_path).calls == 1)
    app.processEvents()
    assert load_project_costs(tmp_path).total_usd == pytest.approx(1.5)
    assert "Costo dell'analisi: 1.50 USD" in line.text()
    first, _, rest = line.toolTip().partition("\n")
    assert first and "1 chiamata a pagamento" in rest  # singolare corretto (audit 25/09)
    assert "Costo totale del progetto: 1.50 USD" in rest
    line.detach()
    _dispose(line)


def test_without_a_project_name_every_dropped_cost_is_reported_not_lost_silently(app):
    # Contract changed (audit D7, 2026-09-25): every dropped cost gets its own
    # log line with its amount. The former single de-duplicated line hid how
    # many paid calls went unrecorded.
    from kicadgen.gui.ai_account import AiAccountLine
    log = []
    line = AiAccountLine(lambda: None, log.append)
    for cost in (0.1, 0.2):
        line._on_paid_call({"cost_usd": cost})
    assert len(log) == 2 and all("non ha un nome" in entry for entry in log)
    assert "0.1000" in log[0] and "0.2000" in log[1]
    line.detach()
    _dispose(line)


def test_the_balance_is_never_read_unless_the_application_enables_it(app, monkeypatch):
    """Negativo: costruire la riga (test, finestre di prova) non tocca la rete."""
    from kicadgen.gui.ai_account import AiAccountLine
    monkeypatch.setattr(openrouter, "credit_status",
                        lambda **k: pytest.fail("rete toccata"))
    line = AiAccountLine(lambda: None, lambda m: None)
    line.refresh_balance()
    assert "Saldo" not in line.text() and line.text() == "—"
    line.detach()
    _dispose(line)


def test_the_balance_is_read_in_background_and_shown(app, monkeypatch):
    from kicadgen.gui.ai_account import AiAccountLine
    monkeypatch.setattr(openrouter, "api_key", lambda: "chiave-finta")
    monkeypatch.setattr(openrouter, "credit_status", lambda **k: openrouter.CreditStatus(
        account_remaining=11.92, total_credits=20, total_usage=8.08))
    line = AiAccountLine(lambda: None, lambda m: None)
    line.balance_enabled = True
    line.refresh_balance()
    _wait(app, lambda: "11.92" in line.text())
    assert line.text() == "Saldo OpenRouter: 11.92 USD"
    line.detach()
    _dispose(line)


def test_an_unreadable_balance_says_not_available(app, monkeypatch):
    from kicadgen.gui.ai_account import AiAccountLine

    def fail(**k):
        raise openrouter.OpenRouterError("connessione fallita")
    monkeypatch.setattr(openrouter, "api_key", lambda: "chiave-finta")
    monkeypatch.setattr(openrouter, "credit_status", fail)
    line = AiAccountLine(lambda: None, lambda m: None)
    line.balance_enabled = True
    line.refresh_balance()
    _wait(app, lambda: "non disponibile" in line.text())
    assert "connessione fallita" in line.toolTip()
    line.detach()
    _dispose(line)


def test_detach_only_removes_its_own_listener(app):
    from kicadgen.gui.ai_account import AiAccountLine
    first = AiAccountLine(lambda: None, lambda m: None)
    second = AiAccountLine(lambda: None, lambda m: None)
    first.detach()  # non e' piu' l'ascoltatore: non tocca quello del secondo
    assert openrouter._usage_listener is not None
    second.detach()
    _dispose(first)
    _dispose(second)
    assert openrouter._usage_listener is None


def test_the_project_tab_shows_the_line_in_the_ai_tools_box(qapp, tmp_path, monkeypatch):
    """La riga segue il progetto: nome cambiato -> costo dell'altra cartella."""
    from kicadgen.gui import main_window as gui
    from qt_fixtures import PreviewStub
    app = qapp
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    window = gui.MainWindow()
    try:
        start_run(tmp_path / "SCHEDA_A")
        add_call(tmp_path / "SCHEDA_A", {"cost_usd": 2.14})
        window.field_name.setText("SCHEDA_A")
        assert window.ai_account.text() == "Costo dell'analisi: 2.14 USD"
        window.field_name.setText("SCHEDA_B")
        assert window.ai_account.text() == "Costo dell'analisi: nessuna chiamata a pagamento"
        # Contratto cambiato di proposito (utente, 2026-09-24): la riga e'
        # l'ultima del modulo dentro «Strumenti AI», non sotto il pulsante.
        from PyQt6.QtWidgets import QFormLayout
        form = window._models_form
        assert window.group_ai_tools.isAncestorOf(window.ai_account)
        row, role = form.getWidgetPosition(window.ai_account)
        assert row == form.rowCount() - 1 and role == QFormLayout.ItemRole.FieldRole
        assert form.labelForField(window.ai_account) is window.label_ai_account
        assert not window.group_ai_tools.isAncestorOf(window.btn_extract_ai)
    finally:
        window.close()
        app.processEvents()
        window.deleteLater()
        app.processEvents()
    assert openrouter._usage_listener is None  # chiusa la finestra, nessun ascoltatore


def _dispose(widget):
    """Distruzione ordinata: un widget Qt lasciato al garbage collector puo'
    essere distrutto dopo la QApplication e far cadere il processo all'uscita
    (codice 139, visto il 2026-09-24)."""
    widget.close()
    widget.setParent(None)
    widget.deleteLater()
    QApplication.processEvents()


# ── Correzioni dell'audit del 2026-09-25 ────────────────────────────────────

def test_a_file_without_since_is_still_counted(tmp_path):
    """Negativo al vecchio KeyError: «since» manca in un file scritto a mano."""
    (tmp_path / COSTS_FILE).write_text(json.dumps({
        "format": "kicadgen-ai-costs", "version": 1, "total_usd": 1.0, "calls": 1,
        "calls_without_cost": 0, "entries": []}), encoding="utf-8")
    costs = add_call(tmp_path, {"cost_usd": 0.5})
    assert costs.calls == 2 and costs.total_usd == pytest.approx(1.5) and costs.since == ""


def test_a_briefly_busy_file_is_retried(tmp_path, monkeypatch):
    """Positivo: due PermissionError (antivirus, indicizzatore), poi la scrittura riesce."""
    import os
    from kicadgen.services import project_costs
    real, failures = os.replace, [2]

    def flaky(src, dst):
        if failures[0]:
            failures[0] -= 1
            raise PermissionError(13, "file in uso")
        return real(src, dst)
    monkeypatch.setattr(project_costs.os, "replace", flaky)
    monkeypatch.setattr(project_costs, "RETRY_FIRST_PAUSE_S", 0)
    assert add_call(tmp_path, {"cost_usd": 0.25}).calls == 1
    assert not list(tmp_path.glob(".ai-costs-*"))


def test_a_file_that_stays_busy_keeps_the_call_for_the_next_one(app, tmp_path, monkeypatch):
    """Negativo: file sempre occupato -> la chiamata resta in coda, non si perde."""
    from kicadgen.gui.ai_account import AiAccountLine
    from kicadgen.services import project_costs
    busy = [True]
    real = project_costs.os.replace

    def replace(src, dst):
        if busy[0]:
            raise PermissionError(13, "file in uso")
        return real(src, dst)
    monkeypatch.setattr(project_costs.os, "replace", replace)
    monkeypatch.setattr(project_costs, "RETRY_FIRST_PAUSE_S", 0)
    log = []
    line = AiAccountLine(lambda: tmp_path, log.append)
    line._on_paid_call({"cost_usd": 0.1})
    assert not (tmp_path / COSTS_FILE).exists() and len(line._pending) == 1
    assert any("attesa" in entry for entry in log)
    busy[0] = False
    line._on_paid_call({"cost_usd": 0.2})
    costs = load_project_costs(tmp_path)
    assert costs.calls == 2 and costs.total_usd == pytest.approx(0.3) and not line._pending
    line.detach()
    _dispose(line)


def test_an_unexpected_error_in_the_slot_never_escapes(app, tmp_path, monkeypatch):
    """Un'eccezione fuori da uno slot chiude PyQt6: qui diventa una riga di log."""
    from kicadgen.gui import ai_account
    monkeypatch.setattr(ai_account, "add_call", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    log = []
    line = ai_account.AiAccountLine(lambda: tmp_path, log.append)
    line._on_paid_call({"cost_usd": 0.1})
    assert len(log) == 1
    line.detach()
    _dispose(line)


def test_a_partly_read_balance_is_not_called_unlimited(app, monkeypatch):
    """Negativo: /credits fallito e chiave senza tetto -> «non disponibile», col motivo."""
    from kicadgen.gui.ai_account import AiAccountLine
    monkeypatch.setattr(openrouter, "api_key", lambda: "chiave-finta")
    monkeypatch.setattr(openrouter, "credit_status", lambda **k: openrouter.CreditStatus(
        account_error="HTTP 500"))
    line = AiAccountLine(lambda: None, lambda m: None)
    line.balance_enabled = True
    line.refresh_balance()
    _wait(app, lambda: "Saldo" in line.text() and "lettura" not in line.text())
    assert line.text() == "Saldo OpenRouter: non disponibile" and "HTTP 500" in line.toolTip()
    line.detach()
    _dispose(line)


def test_a_key_without_ceiling_and_a_read_account_can_still_be_unlimited():
    """Positivo al caso sopra: letti entrambi, nessun limite e' un dato vero."""
    status = openrouter.CreditStatus()
    assert status.remaining is None and status.account_error == ""


def test_money_and_plural_are_written_right(app, tmp_path):
    from kicadgen.gui.ai_account import AiAccountLine, format_usd
    assert format_usd(0.00001) == "< 0.0001"
    assert format_usd(0.0) == "0.0000"
    assert format_usd(0.8954) == "0.8954" and format_usd(2.144) == "2.14"
    add_call(tmp_path, {"cost_usd": 0.5})
    line = AiAccountLine(lambda: tmp_path, lambda m: None)
    assert "1 chiamata a pagamento" in line.toolTip()
    add_call(tmp_path, {"cost_usd": 0.5})
    line.refresh()
    assert "2 chiamate a pagamento" in line.toolTip()
    line.detach()
    _dispose(line)


def test_the_limiting_factor_is_translated():
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    entry = DEFAULT_TRANSLATIONS["ui:saldo dell'account"]
    assert entry["en"] and entry["en"] != "saldo dell'account"


# ── lock file (finding S9) ─────────────────────────────────────────────────

def _old_lock(tmp_path, content=b"999999"):
    """Write a lock file older than LOCK_STALE_S and return its path."""
    import os
    from kicadgen.services import project_costs
    lock = tmp_path / project_costs.LOCK_FILE
    lock.write_bytes(content)
    old = time.time() - project_costs.LOCK_STALE_S - 60
    os.utime(lock, (old, old))
    return lock


def test_a_stale_lock_is_removed_and_a_fresh_one_is_kept(tmp_path):
    """@brief Positive: an abandoned lock is removed. Negative: a fresh lock is not."""
    from kicadgen.services import project_costs
    lock = _old_lock(tmp_path)
    assert project_costs._remove_stale_lock(lock) is True
    assert not lock.exists()
    lock.write_bytes(b"123")
    assert project_costs._remove_stale_lock(lock) is False
    assert lock.read_bytes() == b"123"


def test_a_lock_replaced_after_the_stale_judgement_is_not_removed(tmp_path, monkeypatch):
    """@brief Two writers judging the same stale lock: the second must not remove the first's new lock.

    Between the judgement and the removal another writer removes the stale
    lock and takes a fresh one with other content; the check before unlink
    sees the change and leaves it alone.
    """
    from pathlib import Path
    from kicadgen.services import project_costs
    lock = _old_lock(tmp_path)
    real_read = Path.read_bytes
    reads = []

    def read_and_replace(self):
        data = real_read(self)
        reads.append(data)
        if len(reads) == 1:
            # The other writer, right after our judgement.
            self.unlink()
            self.write_bytes(b"4242")
        return data
    monkeypatch.setattr(Path, "read_bytes", read_and_replace)
    assert project_costs._remove_stale_lock(lock) is False
    monkeypatch.undo()
    assert lock.read_bytes() == b"4242"


def test_a_failed_lock_write_closes_the_handle_and_removes_the_lock(tmp_path, monkeypatch):
    """@brief Negative path of the lock: os.write fails.

    The error reaches the caller, the handle is closed (not leaked) and no
    empty lock stays behind to block the next writer. Positive control: the
    next add_call takes the lock and records the call.
    """
    import os
    from kicadgen.services import project_costs
    closed = []
    real_close = os.close

    def failing_write(handle, data):
        raise OSError(28, "No space left on device")

    def tracking_close(handle):
        closed.append(handle)
        real_close(handle)
    monkeypatch.setattr(project_costs.os, "write", failing_write)
    monkeypatch.setattr(project_costs.os, "close", tracking_close)
    with pytest.raises(OSError):
        add_call(tmp_path, {"cost_usd": 0.01, "model": "m"})
    assert len(closed) == 1
    assert not (tmp_path / project_costs.LOCK_FILE).exists()
    monkeypatch.undo()
    assert add_call(tmp_path, {"cost_usd": 0.01, "model": "m"}).calls == 1
