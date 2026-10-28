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
        value records a local path (``legacy_backup`` included), the stored
        defaults differ from ``analysis_profiles.factory_defaults()``, the
        translations are not the bundled catalogue complete in every
        language of ``i18n.LANGUAGES``, or the component types are not the
        built-in ones of ``component_types.COMPONENT_TYPES``.
    @note The check reads the file with a read-only connection.
    @note Translations and component types are verified too (review
        2026-10-27, I10): a seed with a language missing, or with a stale
        built-in type, shipped unnoticed before, because the old check read
        only ``metadata`` and ``ai_defaults``.
    """
    from kicadgen.services.analysis_profiles import PROFILE_FIELDS, factory_defaults

    path = Path(path)
    if not path.is_file() or path.read_bytes()[:16] != b"SQLite format 3\x00":
        raise SeedError(f"Seed database missing or not SQLite: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        defaults = dict(connection.execute("SELECT name, value FROM ai_defaults"))
        translations = connection.execute("SELECT key, language, text FROM translations").fetchall()
        types = connection.execute(
            "SELECT key, label, family, instructions, active, builtin FROM component_types").fetchall()
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
    _verify_translations(translations)
    _verify_component_types(types)


def _verify_translations(rows: list[tuple[str, str, str]]) -> None:
    """Check the translations table against the bundled catalogue, language by language.

    Every key must carry every language of ``i18n.LANGUAGES`` with the
    bundled text, and the table must hold nothing else: the count equals
    the catalogue's. A missing language would show the Italian source text
    to the users of that language; an extra row would be a leftover of the
    machine that built the seed.
    """
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES

    expected = {(key, language): text
                for key, values in DEFAULT_TRANSLATIONS.items()
                for language, text in values.items()}
    stored = {(key, language): text for key, language, text in rows}
    incomplete = sorted(key for key in DEFAULT_TRANSLATIONS
                        if any((key, language) not in stored for language in LANGUAGES))
    if incomplete:
        raise SeedError(f"Seed database translations miss a language for: {incomplete[:5]}"
                        + (" ..." if len(incomplete) > 5 else ""))
    if len(rows) != len(expected):
        raise SeedError(f"Seed database holds {len(rows)} translation rows, "
                        f"the bundled catalogue has {len(expected)}.")
    different = sorted(key for key, text in stored.items() if expected.get(key) != text)
    if different:
        raise SeedError(f"Seed database translations differ from the catalogue: {different[:5]}")


def _verify_component_types(rows) -> None:
    """Check the component types are exactly the built-in ones, active and unchanged.

    The seed must offer every type of ``COMPONENT_TYPES`` with its label and
    instructions as the code defines them, all built-in and active, and no
    type added on the machine that built the seed.
    """
    from kicadgen.services.component_types import COMPONENT_TYPES

    expected = {t.key: t for t in COMPONENT_TYPES}
    stored = {key: (label, family, instructions, active, builtin)
              for key, label, family, instructions, active, builtin in rows}
    if set(stored) != set(expected):
        raise SeedError("Seed database component types differ from the code: "
                        f"missing {sorted(set(expected) - set(stored))}, "
                        f"extra {sorted(set(stored) - set(expected))}")
    stale = sorted(key for key, (label, family, instructions, active, builtin) in stored.items()
                   if (label, instructions, int(active), int(builtin))
                   != (expected[key].label, expected[key].prompt, 1, 1))
    if stale:
        raise SeedError(f"Seed database component types are stale or not built-in: {stale}")


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
