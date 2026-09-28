"""@brief An unreadable inventory answer is retried once; other failures are not (HANDOFF point 137).

User, 2026-09-28: DeepSeek answered the global inventory with tool arguments
that were not JSON, and every body stayed an unclassified hypothesis. The same
request can be answered well the next time: one new paid attempt, declared in
the log; the unreadable text is kept for the stage audit.
"""
import json

import numpy as np
import pytest

from kicadgen.services import openrouter
from kicadgen.services.openrouter import OpenRouterError, ToolArgumentsError
from kicadgen.services.photo_global_inventory import review_global_inventory
from test_photo_global_inventory import _clean_row, _two_row_response, candidate

ITEMS = [candidate(1), dict(candidate(2), _box=[.6, .6, .7, .7])]
GOOD = _two_row_response([_clean_row('R1', 1, [.2, .2, .4, .4]),
                          _clean_row('R2', 2, [.6, .6, .7, .7])])


def _run(replies, tmp_path):
    """Replies in order: an exception instance is raised, a dict returned."""
    calls, logs = [], []

    def fake(**request):
        calls.append(request)
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return json.loads(json.dumps(reply))

    try:
        result = review_global_inventory(np.zeros((60, 120, 3), np.uint8), ITEMS, 40, 20,
                                         'offline/model', fake, logs.append, tmp_path)
    except ValueError as exc:
        result = exc
    return result, calls, logs


def _audit(tmp_path):
    [path] = (tmp_path / 'yolo-audit').glob('global-inventory-*.json')
    return json.loads(path.read_text(encoding='utf-8'))


def test_an_unreadable_answer_is_retried_once_and_the_good_one_is_used(tmp_path):
    broken = ToolArgumentsError("Gli argomenti dello strumento non sono JSON valido.",
                                raw='{"components": [ {"id": "R1",, }')
    result, calls, logs = _run([broken, GOOD], tmp_path)
    assert len(calls) == 2
    assert calls[1] == calls[0]                     # the same request, paid once more
    assert len(result) == 2
    assert all(row['_global_inventory_errors'] == [] for row in result)
    assert any('un solo nuovo tentativo a pagamento' in line for line in logs)
    assert any("il nuovo tentativo e' leggibile" in line for line in logs)


def test_two_unreadable_answers_keep_the_text_for_the_audit(tmp_path):
    first = ToolArgumentsError("non JSON", raw='{"a": 1,,}')
    second = ToolArgumentsError("non JSON", raw='[oops')
    result, calls, logs = _run([first, second], tmp_path)
    assert len(calls) == 2                          # never a third paid call
    assert any("anche il nuovo tentativo e' fallito" in line for line in logs)
    response = _audit(tmp_path)['response']
    assert response['raw_arguments'] == '{"a": 1,,}'
    assert response['retry_raw_arguments'] == '[oops'


@pytest.mark.parametrize('failure', [
    OpenRouterError('HTTP 402: credito insufficiente'),
    OpenRouterError('rete non raggiungibile', unreachable=True),
])
def test_a_refused_or_unreachable_request_is_not_retried(tmp_path, failure):
    """Negative: only an answer that arrived but cannot be read is worth paying again."""
    _result, calls, logs = _run([failure, GOOD], tmp_path)
    assert len(calls) == 1
    assert not any('nuovo tentativo' in line for line in logs)


def test_the_parser_reports_what_the_model_sent():
    payload = {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
        {'function': {'name': 'report', 'arguments': '{"a": 1,,}'}}]}}]}
    with pytest.raises(ToolArgumentsError) as caught:
        openrouter.tool_arguments(payload, 'report')
    assert caught.value.raw == '{"a": 1,,}'
    assert isinstance(caught.value, OpenRouterError)   # old callers keep catching it
    assert 'JSONDecodeError' in str(caught.value)
