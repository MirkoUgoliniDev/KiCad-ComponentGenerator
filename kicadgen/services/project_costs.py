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
import os
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.validation import finite_number

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
#: A lock whose modification time is farther than this (s) from now, in
#: EITHER direction, is left over by a crashed process and removed: a time in
#: the future (clock skew with a network share, a project folder copied with
#: its lock) would otherwise never age and block the file for ever (audit
#: 138, B2). Heuristic bound, not a measurement: one read-modify-write of this
#: small file takes milliseconds, so a holder alive for this long does not
#: exist in practice; a lock-aware file API would make the bound unnecessary.
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
    #: Number of the analysis these run_* figures describe: the current one
    #: (start_run), or the one asked to `load_project_costs`; 0 when none was
    #: started since the file records analyses (HANDOFF point 135).
    run: int = 0
    #: When that analysis started (ISO, UTC); "" without one, or when the
    #: file has no start record for it (hand-edited file).
    run_started: str = ""
    #: Paid calls of the current analysis: every call recorded after its
    #: start, reruns included, until the next start_run.
    run_usd: float = 0.0
    run_calls: int = 0
    run_calls_without_cost: int = 0
    #: provider -> (total_usd, calls, calls_without_cost) of the current analysis.
    run_by_provider: dict = None

    @property
    def run_complete(self) -> bool:
        """@brief Tell whether every call of the current analysis reported its cost.

        @return True when no call of the current analysis lacks a cost.
        """
        return self.run_calls_without_cost == 0

    @property
    def complete(self) -> bool:
        """@brief Tell whether every recorded call reported its cost.

        @return True when no recorded call lacks a cost.
        """
        return self.calls_without_cost == 0


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _entry_cost(entry: dict) -> Optional[float]:
    """Cost of an entry, None when missing, damaged or negative (audit 138, B6).

    A negative cost is not a refund: it is a damaged (hand-edited) entry, and
    counting it would lower a total that must never be understated.
    """
    cost = finite_number(entry.get("cost_usd"))
    return None if cost is None or cost < 0 else cost


def _read(path: Path) -> Optional[dict]:
    """Validated content of the file, or None when it does not exist."""
    if not path.exists():
        return None
    # OSError (file busy for a moment) goes up as it is: it is not a damaged
    # file and must never be reported, or treated, as one.
    return _parse(path, path.read_text(encoding="utf-8"))


#: Totals of the cost files read by load_project_costs:
#: path -> (raw bytes, {run: ProjectCosts}). Keyed on the bytes themselves, not on the
#: modification time, so a rewrite within the file system's time resolution
#: is never hidden. The status bar reads the file on every refresh, and
#: pricing the DeepSeek entries grows with the history.
_LOADED = {}
_LOADED_LOCK = threading.Lock()
#: Memory bound of _LOADED (projects opened in one session), not a tuning value.
_LOADED_MAX = 32


def _loaded_totals(path: Path, run: Optional[int]) -> Optional[ProjectCosts]:
    """Totals of the file at `path` for `run`, reused while the file bytes are unchanged; None when absent.

    The read-modify-write paths use `_read`, which always parses afresh.
    The returned value is a copy: its provider dicts are not shared.
    """
    if not path.exists():
        return None
    raw = path.read_bytes()
    key = str(path)
    with _LOADED_LOCK:
        cached = _LOADED.get(key)
        totals = cached[1].get(run) if cached is not None and cached[0] == raw else None
    if totals is None:
        # Same decoding errors as read_text; JSON ignores the newline style.
        totals = _totals(_parse(path, raw.decode("utf-8")), run)
        with _LOADED_LOCK:
            cached = _LOADED.get(key)
            if cached is None or cached[0] != raw:
                if cached is None and len(_LOADED) >= _LOADED_MAX:
                    _LOADED.pop(next(iter(_LOADED)))
                cached = _LOADED[key] = (raw, {})
            cached[1][run] = totals
    return replace(totals, by_provider=dict(totals.by_provider),
                   run_by_provider=dict(totals.run_by_provider))


def _parse(path: Path, text: str) -> dict:
    """Validate and price the text of a cost file; ProjectCostsError when it is not usable."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProjectCostsError(f"{path.name}: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ProjectCostsError(f"{path.name}: unknown format")
    version = data.get("version")
    if _count(version) is None or version > VERSION:
        raise ProjectCostsError(f"{path.name}: unsupported version {version!r}")
    total = finite_number(data.get("total_usd"))
    calls = _count(data.get("calls"))
    missing = _count(data.get("calls_without_cost"))
    entries = data.get("entries")
    run = data.get("run", 0)
    if _count(run) is None or not isinstance(data.get("runs", []), list):
        raise ProjectCostsError(f"{path.name}: invalid analyses")
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
        old = _entry_cost(entry)
        if old is not None and entry.get("cost_source") != "balance_delta":
            continue
        prompt = _count(entry.get("prompt_tokens"))
        output = _count(entry.get("completion_tokens"))
        cost = None
        # Both counts must be recorded and valid (audit 138, B5): None is a
        # count that was not reported, and zero on both is how such calls
        # were recorded before None existed. Pricing either would turn an
        # unknown cost into a known, lower one.
        if prompt is not None and output is not None and (prompt or output):
            try:
                moment = datetime.fromisoformat(str(entry.get("time")))
                cost = call_cost(provider_model_name(model),
                                 {"prompt_tokens": prompt, "completion_tokens": output}, moment)
            except (ValueError, OverflowError, OSError, TypeError):
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


def _totals(data: dict, run: Optional[int] = None) -> ProjectCosts:
    """Totals of a validated file content; run_* of `run` (default: the current one)."""
    if run is None or _count(run) is None or run == 0 or run > data.get("run", 0):
        run = data.get("run", 0)
    started = ""
    for entry in data.get("runs", ()):
        if isinstance(entry, dict) and entry.get("run") == run:
            started = str(entry.get("started") or "")
    current = _by_provider(data, run=run) if run else {}
    return ProjectCosts(total_usd=float(data["total_usd"]), calls=data["calls"],
                        calls_without_cost=data["calls_without_cost"],
                        since=data.get("since", ""), by_provider=_by_provider(data),
                        estimated_calls=sum(1 for entry in data["entries"]
                                            if isinstance(entry, dict) and entry.get(
                                                "cost_source") == "token_price_estimate"),
                        run=run, run_started=started,
                        run_usd=sum(total for total, _calls, _missing in current.values()),
                        run_calls=sum(calls for _total, calls, _missing in current.values()),
                        run_calls_without_cost=sum(
                            missing for _total, _calls, missing in current.values()),
                        run_by_provider=current)


def load_project_costs(project_dir: Path, run: Optional[int] = None) -> ProjectCosts:
    """@brief Read the totals of a project; zero when nothing was paid yet.

    @param project_dir Project folder.
    @param run Analysis whose run_* figures are wanted (the one this
        application instance started, see `start_run`); None, or a number the
        file does not know, gives the file's current analysis. Lets an
        instance keep showing its own analysis while a second instance
        starts another one on the same project (audit 138, B8).
    @return The totals (all zero and empty `since` when the file is absent).
    @throws ProjectCostsError When the file exists but is not usable.
    @throws OSError When the file exists but cannot be read right now.
    @note The parsed content is reused while the file's bytes are unchanged
        (the file is still read every time, never its modification time
        alone), so a refresh does not re-parse and re-price the history.
    """
    totals = _loaded_totals(Path(project_dir) / COSTS_FILE, run)
    return ProjectCosts(by_provider={}) if totals is None else totals


def _by_provider(data: dict, run: Optional[int] = None) -> dict:
    """Totals per provider from the entries (a damaged entry is skipped).

    With `run`, only the entries recorded during that analysis.
    """
    from .providers import provider_of
    totals = {}
    for entry in data.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        if run is not None and entry.get("run") != run:
            continue
        provider = provider_of(str(entry.get("model") or ""))
        total, calls, missing = totals.get(provider, (0.0, 0, 0))
        cost = _entry_cost(entry)
        totals[provider] = (total + (cost or 0.0), calls + 1, missing + (cost is None))
    return totals


def add_call(project_dir: Path, record: dict, now: Optional[datetime] = None) -> ProjectCosts:
    """@brief Add one paid call to the project total and save it atomically.

    @param project_dir Project folder; created if missing.
    @param record Call as reported by `openrouter.set_usage_listener`:
        ``cost_usd`` (None when not reported), ``prompt_tokens``,
        ``completion_tokens`` (None when not reported: stored as None, never
        as 0), ``model``, optional ``cost_source`` (how the cost was obtained
        when the provider did not report it, e.g. "token_price" for
        DeepSeek), optional ``run``: the analysis the call belonged to WHEN
        IT WAS MADE (see `start_run`). Without ``run`` the call goes to the
        file's current analysis, as before; a ``run`` the file does not know
        is ignored the same way.
    @param now Time of the call -- the request, not the write, which may
        come later when the file was busy (UTC now by default).
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
    return _locked_update(Path(project_dir), lambda folder: _add_call_once(folder, record, now))


def start_run(project_dir: Path, now: Optional[datetime] = None) -> ProjectCosts:
    """@brief Start a new analysis: calls recorded from now on belong to it.

    @param project_dir Project folder; created if missing.
    @param now Start time (UTC now by default).
    @return The totals, with the new analysis empty: ``.run`` is its number,
        to stamp on every call of this analysis (``record["run"]`` of
        `add_call`), so a call written late -- or after another instance
        started a newer analysis -- still lands in its own (audit 138, B8).
    @throws ProjectCostsError, OSError As `add_call`, with the same guarantees.
    @note The GUI shows the cost of the current analysis, not the project
        total (user decision 2026-09-28): one analysis runs from one «Estrai
        parametri con AI» to the next, the paid reruns in between included.
    """
    return _locked_update(Path(project_dir), lambda folder: _start_run_once(folder, now))


def _locked_update(folder: Path, update) -> ProjectCosts:
    """Run one read-modify-write under the lock, retrying a busy file."""
    pause = RETRY_FIRST_PAUSE_S
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with _exclusive_lock(folder):
                return update(folder)
        except PermissionError:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(pause)
            pause *= 2
    raise AssertionError("unreachable")


def _empty(stamp: str) -> dict:
    """Content of a cost file with nothing recorded yet."""
    return {"format": FORMAT, "version": VERSION, "since": stamp, "total_usd": 0.0,
            "calls": 0, "calls_without_cost": 0, "entries": []}


def _start_run_once(folder: Path, now: Optional[datetime]) -> ProjectCosts:
    """One read-modify-write that opens a new analysis (see `start_run`)."""
    path = folder / COSTS_FILE
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    data = _read(path) or _empty(stamp)
    data["run"] = data.get("run", 0) + 1
    data.setdefault("runs", []).append({"run": data["run"], "started": stamp})
    return _save(folder, path, data)


def _lock_identity(stat_result: os.stat_result) -> tuple:
    """What tells one lock file from another created later under the same name."""
    return (stat_result.st_mtime_ns, stat_result.st_size, stat_result.st_ino)


def _remove_stale_lock(path: Path) -> bool:
    """Remove the lock at `path` if it is stale and still the one judged stale.

    Two writers may judge the same abandoned lock stale at once: the first
    removes it and takes a fresh lock, and the second must not then remove
    that fresh lock. So the lock is re-read just before the removal, and
    removed only when its identity (modification time, size, file id) and
    its content (the holder's pid) are still those that were judged stale.

    @return True when the caller should try to take the lock again at once
        (stale lock removed, or already gone); False to wait and poll.
    @note The re-check narrows the race to the instant between the second
        stat and the unlink; Windows offers no compare-and-delete. A holder
        would have to create, write and keep a lock with the same timestamp,
        size and file id within that instant.
    """
    try:
        judged = path.stat()
        if abs(time.time() - judged.st_mtime) <= LOCK_STALE_S:
            return False
        content = path.read_bytes()
        if _lock_identity(path.stat()) != _lock_identity(judged) or path.read_bytes() != content:
            return False   # replaced meanwhile: another writer's live lock
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True        # released between the calls: try again at once
    except OSError:
        return False


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
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _remove_stale_lock(self.path):
                    continue
            except PermissionError:
                # Windows reports a file being deleted by its holder this way.
                pass
            else:
                try:
                    os.write(handle, str(os.getpid()).encode("ascii"))
                except BaseException:
                    # The lock was created but not written: close and remove
                    # it, so neither the handle nor an empty lock is leaked.
                    os.close(handle)
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    raise
                self.handle = handle
                return self
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
    data = _read(path) or _empty(stamp)
    cost = _entry_cost(record)
    data["version"] = VERSION
    data["calls"] += 1
    if cost is None:
        data["calls_without_cost"] += 1
    else:
        data["total_usd"] = float(data["total_usd"]) + cost
    entry = {
        "time": stamp, "model": str(record.get("model") or ""), "cost_usd": cost,
        # None when not reported or unusable (audit 138, B5).
        "prompt_tokens": _count(record.get("prompt_tokens")),
        "completion_tokens": _count(record.get("completion_tokens")),
    }
    if cost is not None and isinstance(record.get("cost_source"), str):
        entry["cost_source"] = record["cost_source"]
    run = _count(record.get("run"))
    if run and run <= data.get("run", 0):
        entry["run"] = run          # the analysis of the call, stamped when it was made
    elif data.get("run"):
        entry["run"] = data["run"]
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
