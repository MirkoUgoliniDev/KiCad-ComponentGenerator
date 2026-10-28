"""@brief Download the ONNX weights of the Windows setup into models/yolo, verified by SHA-256.

Usage: python tools/fetch_model_weights.py [--manifest packaging/model-weights.json] [--target models]

The weights are not in Git (size, history); they live as assets of the
permanent GitHub release «models». The release workflow runs this tool on a
clean runner before `build-windows.ps1 -IncludeYoloModel`, and a developer
can run it after a fresh clone. Every file is checked against the SHA-256 of
`packaging/model-weights.json`, the same digests THIRD_PARTY_NOTICES.md (and,
for the detectors, `yolo_models.KNOWN_MODELS`) record: a file that differs
is deleted and the tool fails, so a replaced or truncated asset can never
reach a setup. Each entry lands in `<target>/<dir>` (`dir` defaults to
`yolo`; the SMD network says `smd`).

@note Standard library only: the release runner has no project virtualenv
    when this runs. A file already present with the right digest is kept
    without a download; a present file with a wrong digest is replaced.
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
    @return The `weights` entries, each with `file`, `dir`, `sha256` and `url`.
    @throws ValueError When an entry lacks a field, names a path instead of
        a file or folder name, repeats a file, or has a digest that is not
        64 hex characters: such a manifest could write outside the target or
        skip the check.
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
        digest = entry["sha256"].lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"{path}: '{name}' has no valid SHA-256")
        if not entry["url"].startswith("https://"):
            raise ValueError(f"{path}: '{name}' must be fetched over https")
        entry["sha256"] = digest
    return entries


def fetch(entry: dict, target: Path, opener=None, log=print) -> str:
    """@brief Ensure one weights file is in `target` with the manifest's digest.

    @param entry A manifest entry (`file`, `dir`, `sha256`, `url`).
    @param target The models folder; the file lands in `target / entry["dir"]`.
    @param opener Callable returning a readable response for a URL; None
        means `urllib.request.urlopen`, looked up at call time (the tests
        pass a fake one or patch the module).
    @param log Callable receiving one progress line per file.
    @return "kept" when the file was already there with the right digest,
        "downloaded" otherwise.
    @throws ValueError When the downloaded bytes do not match the digest;
        nothing is left in `target` in that case.
    """
    opener = opener or urllib.request.urlopen
    target = target / entry.get("dir", DEFAULT_DIR)
    destination = target / entry["file"]
    if destination.is_file() and sha256_of(destination) == entry["sha256"]:
        log(f"{entry['file']}: already present, SHA-256 verified.")
        return "kept"
    target.mkdir(parents=True, exist_ok=True)
    # Download beside the destination, then rename: a failed or wrong
    # download never leaves a half file under the final name.
    with tempfile.NamedTemporaryFile(dir=target, prefix=entry["file"] + ".", suffix=".part",
                                     delete=False) as handle:
        partial = Path(handle.name)
        try:
            with opener(entry["url"]) as response:
                for chunk in iter(lambda: response.read(_CHUNK), b""):
                    handle.write(chunk)
        except Exception:
            handle.close()
            partial.unlink(missing_ok=True)
            raise
    digest = sha256_of(partial)
    if digest != entry["sha256"]:
        partial.unlink(missing_ok=True)
        raise ValueError(f"{entry['file']}: SHA-256 {digest} differs from the manifest's "
                         f"{entry['sha256']}; the asset is not the recorded file")
    destination.unlink(missing_ok=True)
    partial.replace(destination)
    log(f"{entry['file']}: downloaded into {target.name} ({destination.stat().st_size / 1_000_000:.1f} MB), "
        "SHA-256 verified.")
    return "downloaded"


def main(argv) -> int:
    """@brief Command-line entry point; return the process exit status.

    @param argv Command-line arguments after the program name.
    @return 0 when every file is present and verified, 1 otherwise. The
        first failure stops the run: a setup must not be built with a
        partial or unverified set.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--target", type=Path, default=TARGET)
    args = parser.parse_args(argv)
    try:
        entries = load_manifest(args.manifest)
        for entry in entries:
            fetch(entry, args.target)
    except Exception as exc:  # the reason is what the log needs
        print(f"Model weights not fetched: {exc}", file=sys.stderr)
        return 1
    print(f"{len(entries)} weights files ready in {args.target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
