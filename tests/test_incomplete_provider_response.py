"""Truncated HTTP bodies are paid attempts with unknown cost, never free calls."""
from http.client import IncompleteRead

import pytest

from kicadgen.services import openrouter, providers


@pytest.mark.parametrize('model', ['test/model', 'deepseek-api/deepseek-flash'])
def test_incomplete_http_body_records_one_attempt_without_cost(monkeypatch, model):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            raise IncompleteRead(b'private provider output', 100)

    records = []
    monkeypatch.setattr(openrouter, 'api_key', lambda: 'offline-test-key')
    monkeypatch.setattr(providers, 'deepseek_api_key', lambda: 'offline-test-key')
    monkeypatch.setattr(openrouter, '_usage_listener', records.append)
    monkeypatch.setattr(openrouter.urllib.request, 'urlopen', lambda *a, **k: Response())
    openrouter.reset_usage()
    try:
        tool = {'type': 'function', 'function': {'name': 'report', 'parameters': {}}}
        with pytest.raises(openrouter.OpenRouterError) as raised:
            openrouter.call_tool(model, 'system', [], tool)
        assert raised.value.possibly_billed
        assert not raised.value.retryable
        assert 'private provider output' not in str(raised.value)
        totals = openrouter.usage_totals()
        assert (totals.calls, totals.calls_without_cost, totals.cost_usd) == (1, 1, 0)
        assert not totals.complete
        assert len(records) == 1
        assert records[0]['cost_usd'] is None and records[0]['model'] == model
        assert records[0]['prompt_tokens'] is None
    finally:
        openrouter.reset_usage()
