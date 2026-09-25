"""@brief Collect the license files of the build environment for the package.

Usage: python tools/collect_licenses.py <output folder>

Run by scripts/build-windows.ps1 with the ``.venv-build`` interpreter; the
spec ships the folder as ``_internal\\licenses``. Nothing is downloaded: every
file comes from the installed distributions' metadata (``*.dist-info``) and
from the CPython installation the environment was created from.

Layout written:

- ``Python-<version>/LICENSE.txt``: CPython's license, which also carries the
  notices of the libraries shipped with it (OpenSSL, SQLite, libffi, zlib,
  expat, bzip2, ...).
- ``<distribution>-<version>/...``: each distribution's license, notice,
  copying and authors files, keeping their relative paths.
- ``MANIFEST.txt``: one line per distribution with version and declared
  license, plus the exact versions of the runtime libraries.

@note The build environment is a superset of what PyInstaller bundles
    (pytest and PyInstaller's own helpers are listed too): listing more is
    harmless, missing a bundled component is not.
"""
from __future__ import annotations

import re
import shutil
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

#: File names treated as license material (case-insensitive, any extension).
LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|authors)([-_.].*)?$", re.IGNORECASE)


class LicenseCollectionError(RuntimeError):
    """@brief A license that must ship with the package could not be found."""


def _safe(text: str) -> str:
    """A folder name safe on Windows for a distribution name and version."""
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", text)


def _declared_license(dist: metadata.Distribution) -> str:
    """The SPDX expression, else the short License field, else the classifiers."""
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    short = (meta.get("License") or "").strip()
    if short and "\n" not in short and len(short) <= 80:
        return short
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return "; ".join(classifiers) or "not declared in metadata"


def _license_files(dist: metadata.Distribution) -> list[PurePosixPath]:
    """Relative paths of the license material a distribution records."""
    found = []
    for entry in dist.files or []:
        path = PurePosixPath(str(entry).replace("\\", "/"))
        if "__pycache__" in path.parts or path.suffix in (".py", ".pyc", ".pyi"):
            continue
        in_metadata = any(part.endswith(".dist-info") for part in path.parts)
        if LICENSE_NAME.match(path.name) or (in_metadata and "licenses" in path.parts):
            found.append(path)
    return sorted(found)


def runtime_versions() -> dict[str, str]:
    """@brief Versions of the libraries CPython ships in its own folder.

    @return Library name to version, for the interpreter running this tool.
    """
    import sqlite3
    import ssl
    import zlib
    import pyexpat
    versions = {
        "Python": sys.version.split()[0],
        "OpenSSL": ssl.OPENSSL_VERSION,
        "SQLite": sqlite3.sqlite_version,
        "zlib": zlib.ZLIB_VERSION,
        "expat": pyexpat.EXPAT_VERSION,
    }
    # libffi exposes no version at runtime; its notice is in LICENSE.txt.
    return versions


def collect(output: Path, *, distributions: Optional[Iterable[metadata.Distribution]] = None,
            base_prefix: Optional[Path] = None) -> list[str]:
    """@brief Copy every license file into ``output`` and write the manifest.

    @param output Destination folder; it is emptied first (build product).
    @param distributions Distributions to process; default: all installed in
        the running environment.
    @param base_prefix CPython installation holding ``LICENSE.txt``; default
        ``sys.base_prefix``.
    @return Names of the distributions that record no license file (also
        listed in the manifest, to be checked by hand).
    @throws LicenseCollectionError When CPython's ``LICENSE.txt`` is missing:
        the interpreter is always bundled, so its license must always ship.
    """
    output = Path(output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    prefix = Path(base_prefix or sys.base_prefix)
    python_license = prefix / "LICENSE.txt"
    if not python_license.is_file():
        raise LicenseCollectionError(
            f"CPython license not found: {python_license}. Build with a python.org "
            "installation (or set KICADGEN_BUILD_PYTHON to one) so its license ships.")
    versions = runtime_versions()
    python_dir = output / f"Python-{versions['Python']}"
    python_dir.mkdir()
    shutil.copy2(python_license, python_dir / "LICENSE.txt")

    lines = ["# License files collected from the build environment (no network).",
             "# distribution | version | declared license | files", ""]
    without_files = []
    seen = set()
    dists = list(distributions) if distributions is not None else list(metadata.distributions())
    for dist in sorted(dists, key=lambda d: (d.metadata.get("Name") or "").lower()):
        name = dist.metadata.get("Name") or "unknown"
        key = (name.lower(), dist.version)
        if key in seen:
            continue
        seen.add(key)
        folder = output / _safe(f"{name}-{dist.version}")
        copied = 0
        for relative in _license_files(dist):
            source = Path(dist.locate_file(str(relative)))
            if not source.is_file():
                continue
            target = folder.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        if not copied:
            without_files.append(name)
        lines.append(f"{name} | {dist.version} | {_declared_license(dist)} | "
                     f"{copied if copied else 'NONE - check the upstream license by hand'}")
    lines += ["", "# Runtime shipped with CPython (see Python-*/LICENSE.txt)"]
    lines += [f"{library} | {version}" for library, version in versions.items()]
    (output / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return without_files


def main(argv: list[str]) -> int:
    """@brief Command-line entry point.

    @param argv Arguments without the program name: the output folder.
    @return 0 on success, 1 when a mandatory license is missing, 2 on bad usage.
    """
    if len(argv) != 1:
        print("Usage: python tools/collect_licenses.py <output folder>", file=sys.stderr)
        return 2
    try:
        missing = collect(Path(argv[0]))
    except LicenseCollectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if missing:
        print("No license file recorded by: " + ", ".join(missing)
              + " (listed in MANIFEST.txt; check them by hand).")
    print(f"Licenses collected into {argv[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
