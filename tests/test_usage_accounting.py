"""Consuntivo del costo AI: somma, robustezza e isolamento tra elaborazioni.

Nessuna chiamata di rete: si iniettano le risposte come le restituisce
OpenRouter. Il costo e' quello dichiarato dall'API (``usage.cost``), non una
stima sui listini, quindi qui si verifica solo che venga sommato per intero.
"""

import threading

import pytest

from kicadgen.services import openrouter


def response(cost=None, prompt=0, completion=0, cached=0, reasoning=0):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "prompt_tokens_details": {"cached_tokens": cached},
             "completion_tokens_details": {"reasoning_tokens": reasoning}}
    if cost is not None:
        usage["cost"] = cost
    return {"usage": usage}


@pytest.fixture(autouse=True)
def clean_totals():
    openrouter.reset_usage()
    yield
    openrouter.reset_usage()


def test_totals_add_up_across_calls():
    openrouter._record_usage(response(0.0312, prompt=12000, completion=800,
                                      cached=3000, reasoning=250))
    openrouter._record_usage(response(0.0189, prompt=9000, completion=400))
    totals = openrouter.usage_totals()
    assert totals.calls == 2
    assert totals.cost_usd == pytest.approx(0.0501)
    assert (totals.prompt_tokens, totals.completion_tokens) == (21000, 1200)
    assert (totals.cached_tokens, totals.reasoning_tokens) == (3000, 250)
    assert totals.complete


def test_a_call_without_cost_is_declared_underestimated():
    """Meglio un totale dichiarato incompleto di un totale falso."""
    openrouter._record_usage(response(0.02, prompt=10))
    openrouter._record_usage(response(None, prompt=10))
    totals = openrouter.usage_totals()
    assert totals.calls == 2 and totals.calls_without_cost == 1
    assert not totals.complete
    assert "sottostimato" in totals.describe()


@pytest.mark.parametrize("payload", [
    None, {}, {"usage": None}, {"usage": {"cost": "gratis"}},
    {"usage": {"cost": float("nan")}}, {"usage": {"cost": float("inf")}},
    {"usage": {"prompt_tokens": True, "cost": True}},
    {"usage": {"prompt_tokens": -5, "cost": 0.01}},
])
def test_malformed_payloads_never_corrupt_the_total(payload):
    """Una risposta storta non deve inventare token o costi negativi."""
    openrouter._record_usage(payload)
    totals = openrouter.usage_totals()
    assert totals.calls == 1
    assert totals.cost_usd >= 0 and totals.prompt_tokens >= 0


def test_a_snapshot_does_not_keep_changing_underneath_the_caller():
    openrouter._record_usage(response(0.01, prompt=10))
    snapshot = openrouter.usage_totals()
    openrouter._record_usage(response(0.01, prompt=10))
    assert snapshot.calls == 1, "usage_totals ha restituito un alias del vivo"


def test_reset_isolates_one_run_from_the_next():
    """Un'estrazione interrotta non deve lasciare residui nella successiva."""
    openrouter._record_usage(response(0.05, prompt=100))
    openrouter.reset_usage()
    assert openrouter.usage_totals().calls == 0
    assert "nessuna chiamata" in openrouter.usage_totals().describe()


def test_concurrent_calls_do_not_lose_writes():
    """L'estrazione gira in un thread di lavoro mentre la GUI resta viva."""
    def worker():
        for _ in range(250):
            openrouter._record_usage(response(0.001, prompt=1))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    totals = openrouter.usage_totals()
    assert totals.calls == 2000
    assert totals.cost_usd == pytest.approx(2.0)


def test_small_amounts_keep_enough_digits_to_be_readable():
    openrouter._record_usage(response(3.6e-06, prompt=16, completion=2))
    assert "0.000004" in openrouter.usage_totals().describe()


def test_the_request_asks_the_api_for_the_cost(monkeypatch):
    """Senza usage.include la risposta porta i token ma non il costo."""
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices": [{"message": {"tool_calls": [{"function": {"name": "t", "arguments": "{}"}}]}}]}'

    def fake_urlopen(request, timeout=None):
        sent["body"] = __import__("json").loads(request.data.decode())
        return Response()

    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-v1-test")
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", fake_urlopen)
    openrouter.call_tool("m", "p", [], {"type": "function", "function": {"name": "t"}})
    assert sent["body"]["usage"] == {"include": True}
