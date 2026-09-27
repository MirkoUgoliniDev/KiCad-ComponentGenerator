"""@brief Shared helpers for photo-pipeline audit records and normalized boxes.

The photo pipeline leaves JSON audits of every detector and inventory run under
``<photo-cache>/yolo-audit``. They are evidence for a human review, not a
cache: nothing reads them back to decide a result. Without a limit they grow
by megabytes per analysis, so every writer prunes older audits of the same
kind right after writing a new one (see AUDIT_RETENTION_PER_KIND).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from uuid import uuid4

from .messages import tx

#: Retention policy: number of audit files kept per kind (``yolo-*``,
#: ``global-inventory-*``...) in one audit directory. A policy choice, not a
#: measurement: 20 runs cover a working session of repeated analyses on the
#: same project, while the directory stays bounded (a global-inventory audit is
#: typically 0.1-2 MB). Older runs remain reproducible from the response cache.
AUDIT_RETENTION_PER_KIND = 20


def valid_fraction_box(value):
    """@brief Validate a normalized [x0, y0, x1, y1] rectangle in image fractions.

    @param value Candidate rectangle; each coordinate must be a finite
        non-boolean real in [0, 1].
    @return The rectangle as a new list.
    @throws ValueError If the value is missing, non-finite, outside the image,
        empty or inverted.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(tx("rettangolo mancante"))
    # The range test comes before isfinite: an integer too large for a float
    # (10**400) must fail the range, not raise OverflowError.
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1
           or not math.isfinite(v) for v in value):
        raise ValueError(tx("rettangolo non finito/fuori immagine"))
    x0, y0, x1, y1 = value
    if x0 >= x1 or y0 >= y1:
        raise ValueError(tx("rettangolo vuoto/invertito"))
    return list(value)


def audit_safe(value):
    """@brief Represent non-finite provider numbers explicitly in standard JSON.

    @param value Any JSON-like structure (dicts, lists, tuples, scalars).
    @return The same structure with every NaN/infinity replaced by
        ``{"invalid_number": "<repr>"}``.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return {'invalid_number': str(value)}
    if isinstance(value, dict):
        return {k: audit_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [audit_safe(v) for v in value]
    return value


def prune_audits(directory, pattern, keep=AUDIT_RETENTION_PER_KIND, protect=None):
    """@brief Delete the oldest audit files of one kind beyond the retention limit.

    @param directory Audit directory; a missing directory is not an error.
    @param pattern Compiled regular expression matched against whole file names;
        it defines the kind, so audits of other kinds are never touched.
    @param keep Number of newest matching files to keep (at least 1).
    @param protect Optional path that is never deleted, typically the audit
        just written, even if clock skew made it look older.
    @return Number of files deleted.
    @note Best effort: a file that cannot be inspected or removed is skipped,
        because pruning must never make the analysis fail.
    """
    keep = max(1, int(keep))
    protected = Path(protect).resolve() if protect else None
    entries = []
    try:
        for path in Path(directory).iterdir():
            if not pattern.fullmatch(path.name) or not path.is_file():
                continue
            try:
                entries.append((path.stat().st_mtime, path.name, path))
            except OSError:
                continue
    except OSError:
        return 0
    entries.sort(reverse=True)
    removed = 0
    for _, _, path in entries[keep:]:
        if protected is not None and path.resolve() == protected:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def stage_audit_pattern(stage):
    """@brief Return the file-name pattern of the audits written for one stage.

    @param stage Stage name used by write_stage_audit.
    @return Compiled pattern matching ``<stage>-<32 hex>.json`` only.
    """
    return re.compile(re.escape(stage) + r'-[0-9a-f]{32}\.json')


def write_stage_audit(cache_dir, stage, record, log, keep=AUDIT_RETENTION_PER_KIND):
    """@brief Write one audit JSON under ``<cache_dir>/yolo-audit`` and prune its kind.

    @param cache_dir Photo cache directory, or None to skip auditing.
    @param stage Audit kind, used as file-name prefix.
    @param record JSON-like audit content; non-finite numbers are made explicit.
    @param log Progress callback receiving the written path or the failure.
    @param keep Retention limit for this kind (AUDIT_RETENTION_PER_KIND).
    @return Path of the written audit, or None if nothing was written.
    @note A write failure is logged, never raised: the audit is evidence, not
        part of the result.
    """
    if not cache_dir:
        return None
    try:
        path = Path(cache_dir)/'yolo-audit'
        path.mkdir(parents=True, exist_ok=True)
        target = path/f'{stage}-{uuid4().hex}.json'
        target.write_text(json.dumps(audit_safe(record), indent=2, ensure_ascii=False,
                                     allow_nan=False), encoding='utf-8')
    except (OSError, ValueError) as exc:
        log(tx("Log revisione YOLO non scrivibile: {exc}", exc=exc))
        return None
    log(f'Audit {stage}: {target}')
    prune_audits(path, stage_audit_pattern(stage), keep, protect=target)
    return target
