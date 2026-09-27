"""@brief Running total of the paid AI calls made for one project.

The total lives in ``ai-costs.json`` inside the project folder, so it follows
the project when it is renamed or copied. It grows by one entry for every
paid call, with the cost the provider reports (OpenRouter ``usage.cost``,
USD) or, for DeepSeek, which reports none, tokens x official price
(deepseek_pricing): the aborted runs and the unusable answers count too,
because they were paid. Answers served from the local cache never reach a
provider and never reach this file.

@note A file that cannot be read or does not validate is never overwritten:
    the caller gets ProjectCostsError and the file stays as it is, for a
    person to look at. Losing a history silently would be worse than not
    counting one call.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: File name inside the project folder.
COSTS_FILE = "ai-costs.json"
#: Attempts and first pause (s) when Windows keeps the file busy for a moment
#: (antivirus, indexer, a second instance): the pause doubles each time, so
#: the GUI thread waits at most about 0.15 s before the call is queued.
RETRY_ATTEMPTS = 4
RETRY_FIRST_PAUSE_S = 0.02

#: Lock file next to the cost file: it serializes the read-modify-write of
#: two application instances (or two threads) on the same project, which
#: would otherwise both read N calls and both write N + 1.
LOCK_FILE = COSTS_FILE + ".lock"
#: Longest wait (s) for the lock before giving up with TimeoutError (an
#: OSError: the caller keeps the call and writes it later). Short on purpose:
#: the GUI thread writes the file.
LOCK_TIMEOUT_S = 0.5
LOCK_POLL_S = 0.01
#: A lock older than this (s) is left over by a crashed process and removed.
#: Heuristic bound, not a measurement: one read-modify-write of this small
#: file takes milliseconds, so a holder alive for this long does not exist in
#: practice; a lock-aware file API would make the bound unnecessary.
LOCK_STALE_S = 30.0

#: Format marker and version; a newer version is refused, not rewritten.
FORMAT = "kicadgen-ai-costs"
VERSION = 1


class ProjectCostsError(ValueError):
    """@brief The project cost file exists but cannot be used."""


@dataclass(frozen=True)
class ProjectCosts:
    """@brief Totals of the paid AI calls of one project.

    @note `calls_without_cost` counts calls recorded without a cost: when it
        is not zero, `total_usd` is a lower bound. `estimated_calls` counts
        DeepSeek calls costed later from tokens without the cache split:
        each of those is an upper bound.
    """

    total_usd: float = 0.0
    calls: int = 0
    calls_without_cost: int = 0
    since: str = ""
    #: provider -> (total_usd, calls, calls_without_cost), from the entries
    #: (providers.provider_of on each entry's model).
    by_provider: dict = None
    #: DeepSeek calls priced from their recorded tokens without the cache
    #: split (cost_source "token_price_estimate"): upper bounds.
    estimated_calls: int = 0

    @property
    def complete(self) -> bool:
        """@brief Tell whether every recorded call reported its cost."""
        return self.calls_without_cost == 0


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _read(path: Path) -> Optional[dict]:
    """Validated content of the file, or None when it does not exist."""
    if not path.exists():
        return None
    # OSError (file busy for a moment) goes up as it is: it is not a damaged
    # file and must never be reported, or treated, as one.
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProjectCostsError(f"{path.name}: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ProjectCostsError(f"{path.name}: unknown format")
    version = data.get("version")
    if _count(version) is None or version > VERSION:
        raise ProjectCostsError(f"{path.name}: unsupported version {version!r}")
    total = _number(data.get("total_usd"))
    calls = _count(data.get("calls"))
    missing = _count(data.get("calls_without_cost"))
    entries = data.get("entries")
    if (total is None or total < 0 or calls is None or missing is None
            or missing > calls or not isinstance(entries, list)
            or not isinstance(data.get("since", ""), str)):
        raise ProjectCostsError(f"{path.name}: invalid totals")
    _price_deepseek_calls(data)
    return data


def _price_deepseek_calls(data: dict) -> None:
    """Give DeepSeek calls recorded without a price the cost DeepSeek bills them.

    DeepSeek's API reports no cost; the bill is tokens x the official price
    (deepseek_pricing). Calls recorded before the price was computed at call
    time -- without a cost, or with a share of a balance difference
    (cost_source "balance_delta", unreliable: the balance updates late, it
    gave 0.0 on real runs) -- are costed here from their recorded tokens and
    time. Their tokens carry no cache split, so every input token counts as a
    cache miss: an upper bound (cost_source "token_price_estimate").
    Entries recorded with the bare model name («deepseek-flash», before the
    provider prefix existed) go back to the DeepSeek provider: an OpenRouter
    identifier always has a vendor and a slash, a bare name never.
    Done in memory on every read; the file follows at the next write.
    """
    from .deepseek_pricing import call_cost, price_of
    from .providers import DEEPSEEK_PREFIX, PROVIDER_DEEPSEEK, provider_model_name, provider_of
    for entry in data["entries"]:
        if not isinstance(entry, dict):
            continue
        model = str(entry.get("model") or "")
        if "/" not in model and price_of(model) is not None:
            model = entry["model"] = DEEPSEEK_PREFIX + model
        if provider_of(model) != PROVIDER_DEEPSEEK:
            continue
        old = _number(entry.get("cost_usd"))
        if old is not None and entry.get("cost_source") != "balance_delta":
            continue
        prompt = _count(entry.get("prompt_tokens")) or 0
        output = _count(entry.get("completion_tokens")) or 0
        cost = None
        if prompt or output:
            # Zero tokens means the usage was not reported, not a free call:
            # pricing it would turn an unknown cost into a known zero.
            try:
                moment = datetime.fromisoformat(str(entry.get("time")))
                cost = call_cost(provider_model_name(model),
                                 {"prompt_tokens": prompt, "completion_tokens": output}, moment)
            except (ValueError, OverflowError):
                cost = None   # damaged time or count: left without cost, never fatal
        if cost is None:
            if old is not None:
                # Unreliable balance difference and no price: back to «without cost».
                entry["cost_usd"] = None
                entry.pop("cost_source", None)
                data["total_usd"] = max(0.0, float(data["total_usd"]) - old)
                data["calls_without_cost"] = min(data["calls"], data["calls_without_cost"] + 1)
            continue
        if old is None:
            data["calls_without_cost"] = max(0, data["calls_without_cost"] - 1)
        data["total_usd"] = max(0.0, float(data["total_usd"]) - (old or 0.0) + cost)
        entry["cost_usd"], entry["cost_source"] = cost, "token_price_estimate"


def _totals(data: dict) -> ProjectCosts:
    """Totals of a validated file content."""
    return ProjectCosts(total_usd=float(data["total_usd"]), calls=data["calls"],
                        calls_without_cost=data["calls_without_cost"],
                        since=data.get("since", ""), by_provider=_by_provider(data),
                        estimated_calls=sum(1 for entry in data["entries"]
                                            if isinstance(entry, dict) and entry.get(
                                                "cost_source") == "token_price_estimate"))


def load_project_costs(project_dir: Path) -> ProjectCosts:
    """@brief Read the totals of a project; zero when nothing was paid yet.

    @param project_dir Project folder.
    @return The totals (all zero and empty `since` when the file is absent).
    @throws ProjectCostsError When the file exists but is not usable.
    @throws OSError When the file exists but cannot be read right now.
    """
    data = _read(Path(project_dir) / COSTS_FILE)
    if data is None:
        return ProjectCosts(by_provider={})
    return _totals(data)


def _by_provider(data: dict) -> dict:
    """Totals per provider from the entries (a damaged entry is skipped)."""
    from .providers import provider_of
    totals = {}
    for entry in data.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        provider = provider_of(str(entry.get("model") or ""))
        total, calls, missing = totals.get(provider, (0.0, 0, 0))
        cost = _number(entry.get("cost_usd"))
        totals[provider] = (total + (cost or 0.0), calls + 1, missing + (cost is None))
    return totals


def add_call(project_dir: Path, record: dict, now: Optional[datetime] = None) -> ProjectCosts:
    """@brief Add one paid call to the project total and save it atomically.

    @param project_dir Project folder; created if missing.
    @param record Call as reported by `openrouter.set_usage_listener`:
        ``cost_usd`` (None when not reported), ``prompt_tokens``,
        ``completion_tokens``, ``model``, optional ``cost_source`` (how the
        cost was obtained when the provider did not report it, e.g.
        "token_price" for DeepSeek).
    @param now Timestamp of the call (UTC now by default).
    @return The new totals.
    @throws ProjectCostsError When the existing file is not usable: it is
        left untouched and the call is not recorded.
    @throws OSError When the file stays busy after RETRY_ATTEMPTS attempts, or
        TimeoutError (an OSError) when another writer holds the lock longer
        than LOCK_TIMEOUT_S; the caller keeps the call and tries again later.
    @note The read-modify-write runs under an exclusive lock file
        (LOCK_FILE), so two instances writing the same project never lose a
        call to each other.
    """
    pause = RETRY_FIRST_PAUSE_S
    folder = Path(project_dir)
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with _exclusive_lock(folder):
                return _add_call_once(folder, record, now)
        except PermissionError:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(pause)
            pause *= 2
    raise AssertionError("unreachable")


class _exclusive_lock:
    """Exclusive lock file created with O_CREAT | O_EXCL (atomic on local disks)."""

    def __init__(self, folder: Path):
        self.path = folder / LOCK_FILE
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                self.handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.handle, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                        os.unlink(self.path)
                        continue
                except FileNotFoundError:
                    continue  # released between the two calls: try again at once
                except OSError:
                    pass
            except PermissionError:
                # Windows reports a file being deleted by its holder this way.
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{COSTS_FILE}: locked by another writer")
            time.sleep(LOCK_POLL_S)

    def __exit__(self, *exc_info):
        try:
            os.close(self.handle)
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return False


def _add_call_once(folder: Path, record: dict, now: Optional[datetime]) -> ProjectCosts:
    """One read-modify-write of the cost file (see `add_call`)."""
    path = folder / COSTS_FILE
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    data = _read(path) or {"format": FORMAT, "version": VERSION, "since": stamp,
                           "total_usd": 0.0, "calls": 0, "calls_without_cost": 0,
                           "entries": []}
    cost = _number(record.get("cost_usd"))
    if cost is not None and cost < 0:
        cost = None
    data["version"] = VERSION
    data["calls"] += 1
    if cost is None:
        data["calls_without_cost"] += 1
    else:
        data["total_usd"] = float(data["total_usd"]) + cost
    entry = {
        "time": stamp, "model": str(record.get("model") or ""), "cost_usd": cost,
        "prompt_tokens": _count(record.get("prompt_tokens")) or 0,
        "completion_tokens": _count(record.get("completion_tokens")) or 0,
    }
    if cost is not None and isinstance(record.get("cost_source"), str):
        entry["cost_source"] = record["cost_source"]
    data["entries"].append(entry)
    return _save(folder, path, data)


def _save(folder: Path, path: Path, data: dict) -> ProjectCosts:
    """Write the cost file atomically and return its totals."""
    folder.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".ai-costs-", suffix=".json", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return _totals(data)
