"""@brief Paid inventory calls: a truncated answer is never retried, and one run pays at most one extra call.

Audit 138, A5 and A6 (user decision of 2026-09-28: an unreadable answer gets
exactly ONE new paid attempt, declared in the log).
"""
import io
import json

import pytest

from kicadgen.services import openrouter as client
from kicadgen.services.openrouter import OpenRouterError, ToolArgumentsError
from test_inventory_unreadable_retry import GOOD, _run
from test_photo_global_inventory import _clean_row, _two_row_response, _without

PARTIAL = '{"components": [{"id": "R1", "box": [0.2, 0.2'


def _payload(arguments, reason):
    return {"choices": [{"finish_reason": reason, "message": {"tool_calls": [
        {"function": {"name": "report", "arguments": arguments}}]}}]}


# ── A5: truncation is checked before the retryable argument error ───────────

@pytest.mark.parametrize("reason", ["length", "max_tokens"])
def test_partial_arguments_of_a_truncated_answer_are_a_truncation_not_retryable(reason):
    with pytest.raises(OpenRouterError) as caught:
        client.tool_arguments(_payload(PARTIAL, reason), "report")
    assert not isinstance(caught.value, ToolArgumentsError)
    assert "Risposta troncata" in str(caught.value)
    assert f"finish_reason={reason}" in str(caught.value)


def test_the_same_partial_arguments_in_a_complete_answer_stay_retryable():
    """Negative: without the truncation declared, broken JSON is a broken answer."""
    with pytest.raises(ToolArgumentsError) as caught:
        client.tool_arguments(_payload(PARTIAL, "tool_calls"), "report")
    assert "troncata" not in str(caught.value)
    assert caught.value.raw == PARTIAL


def test_a_truncated_answer_with_complete_arguments_is_still_used():
    """Truncation alone is not a failure when the arguments decoded completely."""
    assert client.tool_arguments(_payload('{"a": 1}', "length"), "report") == {"a": 1}


def test_call_tool_raises_the_truncation_for_a_cut_answer(monkeypatch):
    monkeypatch.setenv(client.API_KEY_ENV, "sk-or-v1-" + "0" * 64)
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps(_payload(PARTIAL, "length")).encode()))
    with pytest.raises(OpenRouterError) as caught:
        client.call_tool("vendor/model", "private", [], {"function": {"name": "report"}})
    assert not isinstance(caught.value, ToolArgumentsError)


def test_the_inventory_does_not_pay_again_for_a_truncated_answer(tmp_path):
    truncated = OpenRouterError("Risposta troncata: budget di token esaurito.")
    _result, calls, logs = _run([truncated, GOOD], tmp_path)
    assert len(calls) == 1
    assert not any('nuovo tentativo' in line for line in logs)


# ── A6: at most one extra paid call per inventory run ───────────────────────

def _omitting_response():
    rows = _without(('presence_confidence', 'class_confidence'),
                    dict(_clean_row('R1', 1, [.2, .2, .4, .4]), confidence='high'),
                    dict(_clean_row('R2', 2, [.6, .6, .7, .7]), confidence='low'))
    return _two_row_response(rows)


def test_after_the_unreadable_retry_an_omission_is_declared_not_paid_a_third_time(tmp_path):
    broken = ToolArgumentsError("non JSON", raw='{"a": 1,,}')
    result, calls, logs = _run([broken, _omitting_response(), GOOD], tmp_path)
    assert len(calls) == 2                                   # never a third paid call
    assert any("nessun altro tentativo" in line for line in logs)
    assert not any("che nomina" in line for line in logs)    # no omission retry announced
    assert all(any('omitted required fields' in error for error in row['_global_inventory_errors'])
               for row in result)
    assert all(row['_global_inventory_status'] == 'partial' for row in result)


def test_an_omission_on_the_first_readable_answer_still_gets_its_one_retry(tmp_path):
    """Negative: without an unreadable answer, the single extra call goes to the omission."""
    result, calls, logs = _run([_omitting_response(), GOOD], tmp_path)
    assert len(calls) == 2
    assert 'presence_confidence, class_confidence' in calls[1]['system_prompt']
    assert not any("nessun altro tentativo" in line for line in logs)
    assert all(row['_global_inventory_errors'] == [] for row in result)
