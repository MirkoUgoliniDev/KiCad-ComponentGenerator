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
                                             load_project_costs)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


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


# ── Riga sotto «Estrai parametri con AI» ────────────────────────────────────

def test_a_call_from_a_worker_thread_is_written_into_the_project(app, tmp_path):
    """Positivo, percorso vero: thread di lavoro -> segnale Qt -> file del progetto."""
    from kicadgen.gui.ai_account import AiAccountLine
    log = []
    line = AiAccountLine(lambda: tmp_path, log.append)
    thread = threading.Thread(target=openrouter._record_usage,
                              args=({"model": "m", "usage": {"cost": 1.5}},))
    thread.start()
    thread.join()
    _wait(app, lambda: (tmp_path / COSTS_FILE).exists())
    app.processEvents()
    assert load_project_costs(tmp_path).total_usd == pytest.approx(1.5)
    assert "Costo del progetto: 1.50 USD" in line.text()
    first, _, rest = line.toolTip().partition("\n")
    assert first and "1 chiamate a pagamento" in rest
    line.detach()
    _dispose(line)


def test_without_a_project_name_the_cost_is_reported_once_not_lost_silently(app):
    from kicadgen.gui.ai_account import AiAccountLine
    log = []
    line = AiAccountLine(lambda: None, log.append)
    for _ in range(2):
        line._on_paid_call({"cost_usd": 0.1})
    assert len(log) == 1 and "non ha un nome" in log[0]
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


def test_the_project_tab_shows_the_line_under_the_extract_button(tmp_path, monkeypatch):
    """La riga segue il progetto: nome cambiato -> costo dell'altra cartella."""
    from kicadgen.gui import main_window as gui
    from test_photo_inventory_gui import PreviewStub
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    window = gui.MainWindow()
    try:
        add_call(tmp_path / "SCHEDA_A", {"cost_usd": 2.14})
        window.field_name.setText("SCHEDA_A")
        assert window.ai_account.text() == "Costo del progetto: 2.14 USD"
        window.field_name.setText("SCHEDA_B")
        assert window.ai_account.text() == "Costo del progetto: nessuna chiamata a pagamento"
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
