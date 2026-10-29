"""@brief Download the ONNX weights of the Windows setup into models/yolo, verified by SHA-256.

Usage: python tools/fetch_model_weights.py [--manifest packaging/model-weights.json] [--target models] [--check]

The weights are not in Git (size, history); they live as assets of the
permanent GitHub release «models». The release workflow runs this tool on a
clean runner before `build-windows.ps1 -IncludeYoloModel`, and a developer
can run it after a fresh clone. Every file is checked against the size and
the SHA-256 of `packaging/model-weights.json`, the same digests
THIRD_PARTY_NOTICES.md (and, for the detectors, `yolo_models.KNOWN_MODELS`)
record: a file that differs is deleted and the tool fails, so a replaced or
truncated asset can never reach a setup. Each entry lands in
`<target>/<dir>` (`dir` defaults to `yolo`; the SMD network says `smd`).

With `--check` nothing is downloaded: the tool only verifies that every
manifest file is already present with its digest (`build-windows.ps1
-IncludeYoloModel` runs it before PyInstaller, and the spec ships exactly the
files `verified_weights` returns).

Exit status: 0 when every file is present and verified; 2 when the failure
is permanent (malformed manifest, wrong size or digest, missing file under
`--check`), so retrying is pointless; 1 for any other failure, such as a
network error, which a caller may retry.

@note Standard library only: the release runner has no project virtualenv
    when this runs, and the PyInstaller spec loads this module by path. A
    file already present with the right digest is kept without a download;
    a present file with a wrong digest is replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "model-weights.json"
TARGET = ROOT / "models"
#: Sub-folder of an entry without `dir`: the detectors' (app_paths.YOLO_MODELS_DIR).
DEFAULT_DIR = "yolo"
#: Read the download in pieces: the files are 45-105 MB.
_CHUNK = 1 << 20
#: Seconds urlopen waits for the connection and for each read: a stalled
#: server must fail the step, not hold the runner until the job limit.
DOWNLOAD_TIMEOUT = 60
#: Exit status of a permanent failure (see the module docstring).
EXIT_PERMANENT = 2


def sha256_of(path: Path) -> str:
    """@brief SHA-256 of a file, read in chunks.

    @param path The file to digest.
    @return The lowercase hexadecimal digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> list[dict]:
    """@brief Read and validate the manifest.

    @param path The JSON manifest.
    @return The `weights` entries, each with `file`, `dir`, `size` (bytes),
        `sha256` and `url`.
    @throws ValueError When an entry lacks a field, names a path instead of
        a file or folder name, repeats a file, has a size that is not a
        positive integer, or has a digest that is not 64 hex characters:
        such a manifest could write outside the target or skip the check.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("weights")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'weights' must be a non-empty list")
    seen = set()
    for entry in entries:
        for key in ("file", "sha256", "url"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ValueError(f"{path}: entry without '{key}': {entry}")
        name = entry["file"]
        folder = entry.setdefault("dir", DEFAULT_DIR)
        for value in (name, folder):
            if not isinstance(value, str) or Path(value).name != value or value in (".", ".."):
                raise ValueError(f"{path}: '{value}' is not a plain file or folder name")
        if (folder, name) in seen:
            raise ValueError(f"{path}: '{folder}/{name}' listed twice")
        seen.add((folder, name))
        size = entry.get("size")
        # bool is an int subclass: `true` must not pass for a one-byte file.
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f"{path}: '{name}' has no valid size in bytes")
        digest = entry["sha256"].lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"{path}: '{name}' has no valid SHA-256")
        if not entry["url"].startswith("https://"):
            raise ValueError(f"{path}: '{name}' must be fetched over https")
        entry["sha256"] = digest
    return entries


def _open_with_timeout(url):
    """Open `url` with urllib, bounded by DOWNLOAD_TIMEOUT (urlopen is looked up at call time)."""
    return urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT)


def fetch(entry: dict, target: Path, opener=None, log=print) -> str:
    """@brief Ensure one weights file is in `target` with the manifest's size and digest.

    @param entry A manifest entry (`file`, `dir`, `size`, `sha256`, `url`).
    @param target The models folder; the file lands in `target / entry["dir"]`.
    @param opener Callable taking a URL and returning a readable response
        usable as a context manager; None means `urllib.request.urlopen`
        with a timeout of DOWNLOAD_TIMEOUT seconds, looked up at call time
        (the tests pass a fake one or patch the module).
    @param log Callable receiving one progress line per file.
    @return "kept" when the file was already there with the right digest,
        "downloaded" otherwise.
    @throws ValueError When the download is longer than the manifest's size
        (refused as soon as it exceeds it), shorter than it, or does not
        match the digest.
    @note Whatever ends the download early — an error, a wrong file,
        Ctrl-C, a destination that cannot be replaced — the partial file is
        removed: nothing but a verified file under its final name is ever
        left in `target`.
    """
    opener = opener or _open_with_timeout
    target = target / entry.get("dir", DEFAULT_DIR)
    destination = target / entry["file"]
    if destination.is_file() and sha256_of(destination) == entry["sha256"]:
        log(f"{entry['file']}: already present, SHA-256 verified.")
        return "kept"
    target.mkdir(parents=True, exist_ok=True)
    expected = entry["size"]
    partial = None
    # Download beside the destination, then rename: a failed or wrong
    # download never leaves a half file under the final name.
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=entry["file"] + ".", suffix=".part",
                                         delete=False) as handle:
            partial = Path(handle.name)
            received = 0
            with opener(entry["url"]) as response:
                for chunk in iter(lambda: response.read(_CHUNK), b""):
                    received += len(chunk)
                    if received > expected:
                        raise ValueError(f"{entry['file']}: the download exceeds the manifest's "
                                         f"{expected} bytes; the asset is not the recorded file")
                    handle.write(chunk)
        if received != expected:
            raise ValueError(f"{entry['file']}: {received} bytes downloaded, the manifest records "
                             f"{expected}; the download is truncated")
        digest = sha256_of(partial)
        if digest != entry["sha256"]:
            raise ValueError(f"{entry['file']}: SHA-256 {digest} differs from the manifest's "
                             f"{entry['sha256']}; the asset is not the recorded file")
        # A stale destination that is open elsewhere (Windows) cannot be
        # removed: the except below then drops the verified .part too.
        destination.unlink(missing_ok=True)
        partial.replace(destination)
    except BaseException:
        # BaseException: Ctrl-C (KeyboardInterrupt) must not leave a .part.
        # After a successful replace the .part no longer exists.
        if partial is not None:
            partial.unlink(missing_ok=True)
        raise
    log(f"{entry['file']}: downloaded into {target.name} ({destination.stat().st_size / 1_000_000:.1f} MB), "
        "SHA-256 verified.")
    return "downloaded"


def verified_weights(entries: list[dict], target: Path) -> list[tuple[str, Path]]:
    """@brief The manifest's files as found under `target`, each verified, for the package.

    @param entries Manifest entries from `load_manifest`.
    @param target The models folder (`target / dir / file`).
    @return One (`dir`, path) pair per manifest entry, in manifest order.
        Other files in the folders (an unknown export, a `.pt`) are never
        returned: the setup ships exactly the manifest.
    @throws ValueError Listing every file that is missing or whose size or
        SHA-256 differs from the manifest: a setup must not ship a partial
        or unverified set.
    """
    found, problems = [], []
    for entry in entries:
        folder = entry.get("dir", DEFAULT_DIR)
        path = target / folder / entry["file"]
        if not path.is_file():
            problems.append(f"{folder}/{entry['file']}: missing")
        elif path.stat().st_size != entry["size"]:
            problems.append(f"{folder}/{entry['file']}: {path.stat().st_size} bytes, "
                            f"the manifest records {entry['size']}")
        elif sha256_of(path) != entry["sha256"]:
            problems.append(f"{folder}/{entry['file']}: SHA-256 differs from the manifest")
        else:
            found.append((folder, path))
    if problems:
        raise ValueError("model weights not verified against the manifest: " + "; ".join(problems))
    return found


def main(argv) -> int:
    """@brief Command-line entry point; return the process exit status.

    @param argv Command-line arguments after the program name.
    @return 0 when every file is present and verified; EXIT_PERMANENT (2)
        for a failure a retry cannot fix (manifest, size, digest, a file
        missing under `--check`); 1 otherwise (network). The first failure
        stops the run: a setup must not be built with a partial or
        unverified set.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--target", type=Path, default=TARGET)
    parser.add_argument("--check", action="store_true",
                        help="verify the files already present; download nothing")
    args = parser.parse_args(argv)
    try:
        entries = load_manifest(args.manifest)
        if args.check:
            for folder, path in verified_weights(entries, args.target):
                print(f"{folder}/{path.name}: {path.stat().st_size / 1_000_000:.1f} MB, SHA-256 verified.")
        else:
            for entry in entries:
                fetch(entry, args.target)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError too
        print(f"Model weights not verified: {exc}", file=sys.stderr)
        return EXIT_PERMANENT
    except Exception as exc:  # the reason is what the log needs
        print(f"Model weights not fetched: {exc}", file=sys.stderr)
        return 1
    print(f"{len(entries)} weights files ready in {args.target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
