"""@brief Running total of the paid AI calls made for one project.

The total lives in ``ai-costs.json`` inside the project folder, so it follows
the project when it is renamed or copied. It grows by one entry for every
paid call, as OpenRouter reports it (``usage.cost``, USD): the aborted runs
and the unusable answers count too, because they were paid. Answers served
from the local cache never reach OpenRouter and never reach this file.

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

#: Format marker and version; a newer version is refused, not rewritten.
FORMAT = "kicadgen-ai-costs"
VERSION = 1


class ProjectCostsError(ValueError):
    """@brief The project cost file exists but cannot be used."""


@dataclass(frozen=True)
class ProjectCosts:
    """@brief Totals of the paid AI calls of one project.

    @note `calls_without_cost` counts calls OpenRouter answered without a
        cost: when it is not zero, `total_usd` is a lower bound.
    """

    total_usd: float = 0.0
    calls: int = 0
    calls_without_cost: int = 0
    since: str = ""

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
    return data


def load_project_costs(project_dir: Path) -> ProjectCosts:
    """@brief Read the totals of a project; zero when nothing was paid yet.

    @param project_dir Project folder.
    @return The totals (all zero and empty `since` when the file is absent).
    @throws ProjectCostsError When the file exists but is not usable.
    @throws OSError When the file exists but cannot be read right now.
    """
    data = _read(Path(project_dir) / COSTS_FILE)
    if data is None:
        return ProjectCosts()
    return ProjectCosts(total_usd=float(data["total_usd"]), calls=data["calls"],
                        calls_without_cost=data["calls_without_cost"],
                        since=data.get("since", ""))


def add_call(project_dir: Path, record: dict, now: Optional[datetime] = None) -> ProjectCosts:
    """@brief Add one paid call to the project total and save it atomically.

    @param project_dir Project folder; created if missing.
    @param record Call as reported by `openrouter.set_usage_listener`:
        ``cost_usd`` (None when not reported), ``prompt_tokens``,
        ``completion_tokens``, ``model``.
    @param now Timestamp of the call (UTC now by default).
    @return The new totals.
    @throws ProjectCostsError When the existing file is not usable: it is
        left untouched and the call is not recorded.
    @throws OSError When the file stays busy after RETRY_ATTEMPTS attempts;
        the caller keeps the call and tries again later.
    """
    pause = RETRY_FIRST_PAUSE_S
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return _add_call_once(Path(project_dir), record, now)
        except PermissionError:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(pause)
            pause *= 2
    raise AssertionError("unreachable")


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
    data["entries"].append({
        "time": stamp, "model": str(record.get("model") or ""), "cost_usd": cost,
        "prompt_tokens": _count(record.get("prompt_tokens")) or 0,
        "completion_tokens": _count(record.get("completion_tokens")) or 0,
    })
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
    return ProjectCosts(total_usd=float(data["total_usd"]), calls=data["calls"],
                        calls_without_cost=data["calls_without_cost"],
                        since=data.get("since", ""))
