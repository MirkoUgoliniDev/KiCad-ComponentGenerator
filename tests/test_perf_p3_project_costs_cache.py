"""@brief load_project_costs reuses its parse while the cost file bytes are unchanged.

The status bar reads the cost file on every refresh; parsing and pricing the
history grows with it. The cache is keyed on the file bytes, so a rewrite is
always seen, even one that keeps the size and the modification time.
"""
import os

import pytest

from kicadgen.services import project_costs
from kicadgen.services.project_costs import (COSTS_FILE, ProjectCostsError, add_call,
                                             load_project_costs, start_run)


def _count_parses(monkeypatch):
    calls = []
    original = project_costs._parse

    def counted(path, text):
        calls.append(1)
        return original(path, text)

    monkeypatch.setattr(project_costs, "_parse", counted)
    return calls


def test_unchanged_file_is_parsed_once(tmp_path, monkeypatch):
    start_run(tmp_path)
    for index in range(5):
        add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.01 * (index + 1), "run": 1})
    parses = _count_parses(monkeypatch)
    first = load_project_costs(tmp_path)
    for _ in range(20):
        assert load_project_costs(tmp_path) == first
    assert len(parses) == 1
    assert first.calls == 5 and first.total_usd == pytest.approx(0.15)


def test_cached_totals_equal_a_fresh_read(tmp_path):
    start_run(tmp_path)
    add_call(tmp_path, {"model": "deepseek-api/deepseek-flash", "cost_usd": None,
                        "prompt_tokens": 1000, "completion_tokens": 200, "run": 1})
    add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.02, "run": 1})
    cached = load_project_costs(tmp_path)
    cached = load_project_costs(tmp_path)
    fresh = project_costs._totals(project_costs._read(tmp_path / COSTS_FILE))
    assert cached == fresh


def test_a_same_size_same_time_rewrite_is_seen(tmp_path):
    """Negative case: a cache keyed on size and time alone would hide this edit."""
    add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.25})
    path = tmp_path / COSTS_FILE
    before = load_project_costs(tmp_path)
    stat = path.stat()
    raw = path.read_bytes()
    assert raw.count(b"0.25") == 2  # total_usd and the entry's cost_usd
    edited = raw.replace(b"0.25", b"0.75")
    assert len(edited) == stat.st_size
    path.write_bytes(edited)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = load_project_costs(tmp_path)
    assert before.total_usd == pytest.approx(0.25)
    assert after.total_usd == pytest.approx(0.75)


def test_runs_are_cached_separately(tmp_path):
    start_run(tmp_path)
    add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.1, "run": 1})
    start_run(tmp_path)
    add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.3, "run": 2})
    assert load_project_costs(tmp_path, 1).run_usd == pytest.approx(0.1)
    assert load_project_costs(tmp_path, 2).run_usd == pytest.approx(0.3)
    assert load_project_costs(tmp_path).run_usd == pytest.approx(0.3)
    assert load_project_costs(tmp_path, 1).run_usd == pytest.approx(0.1)


def test_a_mutated_result_does_not_poison_the_cache(tmp_path):
    add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.1})
    first = load_project_costs(tmp_path)
    first.by_provider.clear()
    first.run_by_provider["intruder"] = (1.0, 1, 0)
    second = load_project_costs(tmp_path)
    assert second.by_provider and "intruder" not in second.run_by_provider


def test_a_damaged_file_raises_every_time_and_a_repair_is_seen(tmp_path):
    add_call(tmp_path, {"model": "openai/gpt", "cost_usd": 0.1})
    path = tmp_path / COSTS_FILE
    good = path.read_text(encoding="utf-8")
    load_project_costs(tmp_path)
    path.write_text("{ damaged", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(ProjectCostsError):
            load_project_costs(tmp_path)
    path.write_text(good, encoding="utf-8")
    assert load_project_costs(tmp_path).total_usd == pytest.approx(0.1)
    path.unlink()
    assert load_project_costs(tmp_path).calls == 0
