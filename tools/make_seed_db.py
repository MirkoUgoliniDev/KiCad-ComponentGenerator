"""@brief Build the initial profile database shipped with the Windows package.

Usage: python tools/make_seed_db.py <output .sqlite3>

The seed is generated from the code (``ProfileStore`` on an empty path), never
copied from the repository: the tracked ``data/analysis_profiles.sqlite3`` can
hold stale default prompts and developer metadata (for example a
``legacy_backup`` path of the developer's machine). The result is verified
before the build uses it; any failure exits non-zero so the build stops.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SeedError(RuntimeError):
    """@brief The generated seed database is missing or not clean."""


def verify_seed(path: Path) -> None:
    """@brief Check that a seed database is fresh, generic and complete.

    @param path The seed database to check.
    @throws SeedError When the file is not a SQLite database, a metadata
        value records a local path (``legacy_backup`` included), or the stored
        defaults differ from ``analysis_profiles.factory_defaults()``.
    @note The check reads the file with a read-only connection.
    """
    from kicadgen.services.analysis_profiles import PROFILE_FIELDS, factory_defaults

    path = Path(path)
    if not path.is_file() or path.read_bytes()[:16] != b"SQLite format 3\x00":
        raise SeedError(f"Seed database missing or not SQLite: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        defaults = dict(connection.execute("SELECT name, value FROM ai_defaults"))
    finally:
        connection.close()
    # A fresh store records legacy_backup as an empty string; any other value
    # is a path of the machine that created the file and must not ship.
    dirty = {name: value for name, value in metadata.items() if value}
    if dirty:
        raise SeedError(f"Seed database carries local metadata: {sorted(dirty)}")
    if set(defaults) != set(PROFILE_FIELDS):
        raise SeedError("Seed database defaults are incomplete.")
    expected = factory_defaults()
    # Model names are stored stripped; prompts are stored verbatim.
    stale = sorted(name for name, value in expected.items()
                   if defaults.get(name) not in (value, value.strip()))
    if stale:
        raise SeedError(f"Seed database defaults differ from the code: {stale}")


def make_seed(output: Path) -> Path:
    """@brief Create a fresh profile database at ``output`` and verify it.

    @param output Target file; an existing file (and its SQLite companions) is
        replaced, because it is a build product, never user data.
    @return The path of the verified seed.
    @throws SeedError When the generated file fails ``verify_seed``.
    """
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        companion = output.with_name(output.name + suffix)
        if companion.exists():
            companion.unlink()
    # The explicit path wins over KICADGEN_PROFILE_DB and app_paths; drop the
    # override anyway so nothing else in this process can touch another file.
    os.environ.pop("KICADGEN_PROFILE_DB", None)
    from kicadgen.services.analysis_profiles import ProfileStore
    ProfileStore(path=output)
    verify_seed(output)
    return output


def main(argv: list[str]) -> int:
    """@brief Command-line entry point.

    @param argv Arguments without the program name: the output path.
    @return 0 on success, 1 on a verification failure, 2 on bad usage.
    """
    if len(argv) != 1:
        print("Usage: python tools/make_seed_db.py <output .sqlite3>", file=sys.stderr)
        return 2
    try:
        path = make_seed(Path(argv[0]))
    except SeedError as exc:
        print(f"Seed database rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Seed database ready: {path}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main(sys.argv[1:]))
