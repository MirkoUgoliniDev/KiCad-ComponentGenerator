"""@brief tools/fetch_model_weights.py: verified downloads, never a wrong file in models/yolo.

No network: the opener is a fake. The manifest's digests are also checked
against KNOWN_MODELS and THIRD_PARTY_NOTICES.md, the two other places that
record them, so the three cannot drift apart.
"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

from kicadgen.services import yolo_models

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("fetch_model_weights", ROOT / "tools" / "fetch_model_weights.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(io.BytesIO):
    """A readable response usable as a context manager, like urlopen's."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener(payloads, calls):
    """A fake opener; it also accepts urlopen's `timeout` so it can replace urlopen itself."""
    def open_url(url, timeout=None):
        calls.append(url)
        return _Response(payloads[url])
    return open_url


def _entry(name, payload, url="https://example.invalid/w.onnx"):
    return {"file": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "url": url}


def test_the_manifest_digests_are_the_ones_the_program_and_the_notices_record(tool):
    """@brief Every manifest SHA-256 is a notices entry (the detectors' a KNOWN_MODELS key too); URLs point at the «models» release."""
    entries = tool.load_manifest(ROOT / "packaging" / "model-weights.json")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert len(entries) == 5
    assert {entry["dir"] for entry in entries} == {"yolo", "smd"}
    for entry in entries:
        # The size is the asset's byte count, checked against the release on
        # 2026-10-29 (K3); the files are tens of megabytes.
        assert 10_000_000 < entry["size"] < 200_000_000, entry["file"]
        if entry["dir"] == "yolo":
            assert entry["sha256"] in yolo_models.KNOWN_MODELS, entry["file"]
        assert f"SHA-256: `{entry['sha256']}`" in notices, entry["file"]
        assert entry["url"].endswith("/releases/download/models/" + entry["file"])
        assert entry["file"].endswith(".onnx")


def test_each_entry_lands_in_its_own_sub_folder(tool, tmp_path):
    """@brief Positive: `dir` chooses models/<dir>, and defaults to the detectors' folder."""
    detector, network = _entry("a.onnx", b"a", "https://x/a"), _entry("s.onnx", b"s", "https://x/s")
    network["dir"] = "smd"
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [detector, network]}), encoding="utf-8")
    opener = _opener({"https://x/a": b"a", "https://x/s": b"s"}, [])
    entries = tool.load_manifest(manifest)
    assert entries[0]["dir"] == "yolo" and entries[1]["dir"] == "smd"
    for entry in entries:
        tool.fetch(entry, tmp_path / "models", opener=opener, log=lambda line: None)
    assert (tmp_path / "models" / "yolo" / "a.onnx").read_bytes() == b"a"
    assert (tmp_path / "models" / "smd" / "s.onnx").read_bytes() == b"s"


def test_a_verified_download_lands_and_a_present_file_is_kept(tool, tmp_path):
    """@brief Positive: download once, then the same call keeps the file without a request."""
    payload = b"onnx bytes " * 1000
    entry = _entry("a.onnx", payload)
    calls, lines = [], []
    opener = _opener({entry["url"]: payload}, calls)
    assert tool.fetch(entry, tmp_path, opener=opener, log=lines.append) == "downloaded"
    assert (tmp_path / "yolo" / "a.onnx").read_bytes() == payload
    assert tool.fetch(entry, tmp_path, opener=opener, log=lines.append) == "kept"
    assert calls == [entry["url"]]
    assert not list((tmp_path / "yolo").glob("*.part"))


def test_a_download_with_another_digest_leaves_nothing_behind(tool, tmp_path):
    """@brief Negative: a replaced asset is refused and no file (nor a .part) stays."""
    entry = _entry("a.onnx", b"the recorded file")
    # Same length as the recorded file: the digest, not the size, refuses it.
    opener = _opener({entry["url"]: b"the recorded fil!"}, [])
    with pytest.raises(ValueError, match="SHA-256 .* differs"):
        tool.fetch(entry, tmp_path, opener=opener, log=lambda line: None)
    assert not list((tmp_path / "yolo").iterdir())


def test_a_present_file_with_a_wrong_digest_is_replaced(tool, tmp_path):
    """@brief Positive: a stale or corrupted local file is downloaded again, not trusted."""
    payload = b"the recorded file"
    entry = _entry("a.onnx", payload)
    (tmp_path / "yolo").mkdir()
    (tmp_path / "yolo" / "a.onnx").write_bytes(b"stale")
    calls = []
    assert tool.fetch(entry, tmp_path, opener=_opener({entry["url"]: payload}, calls),
                      log=lambda line: None) == "downloaded"
    assert calls == [entry["url"]] and (tmp_path / "yolo" / "a.onnx").read_bytes() == payload


def test_a_failed_download_leaves_no_part_file(tool, tmp_path):
    """@brief Negative: a network error propagates and the partial file is removed."""
    entry = _entry("a.onnx", b"x")

    def broken(url):
        raise OSError("connection reset")
    with pytest.raises(OSError):
        tool.fetch(entry, tmp_path, opener=broken, log=lambda line: None)
    assert not list((tmp_path / "yolo").iterdir())


@pytest.mark.parametrize("entry, reason", [
    ({"file": "../escape.onnx", "size": 1, "sha256": "0" * 64, "url": "https://x/y"}, "plain file"),
    ({"file": "a.onnx", "dir": "../escape", "size": 1, "sha256": "0" * 64, "url": "https://x/y"}, "plain file"),
    ({"file": "a.onnx", "size": 1, "sha256": "abc", "url": "https://x/y"}, "SHA-256"),
    ({"file": "a.onnx", "size": 1, "sha256": "0" * 64, "url": "http://x/y"}, "https"),
    ({"file": "a.onnx", "size": 1, "sha256": "0" * 64}, "without 'url'"),
    ({"file": "a.onnx", "sha256": "0" * 64, "url": "https://x/y"}, "size"),
    ({"file": "a.onnx", "size": 0, "sha256": "0" * 64, "url": "https://x/y"}, "size"),
    ({"file": "a.onnx", "size": True, "sha256": "0" * 64, "url": "https://x/y"}, "size"),
    ({"file": "a.onnx", "size": "5", "sha256": "0" * 64, "url": "https://x/y"}, "size"),
])
def test_a_malformed_manifest_is_refused(tool, tmp_path, entry, reason):
    """@brief Negative: paths, short digests, plain http, missing fields and bad sizes never reach a download.

    Each case carries every other field valid, so the refusal is for the reason named.
    """
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [entry]}), encoding="utf-8")
    with pytest.raises(ValueError, match=reason):
        tool.load_manifest(manifest)


def test_main_stops_at_the_first_failure(tool, tmp_path, monkeypatch, capsys):
    """@brief Negative: with a wrong asset the exit status is 1 and the later file is not fetched."""
    good, bad = _entry("good.onnx", b"g", "https://x/good"), _entry("bad.onnx", b"b", "https://x/bad")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [bad, good]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(tool.urllib.request, "urlopen", _opener({"https://x/bad": b"o", "https://x/good": b"g"}, calls))
    # CONTRACT CHANGED 2026-10-29 (K4): a wrong digest is permanent, exit 2
    # (the release workflow retries only exit 1, a network failure).
    assert tool.main(["--manifest", str(manifest), "--target", str(tmp_path / "models")]) == tool.EXIT_PERMANENT
    assert calls == ["https://x/bad"] and "differs" in capsys.readouterr().err


# -- review 2026-10-29, K3: timeout, size cap, cleanup on any interruption ------


class _EndlessResponse(_Response):
    """A response that never ends: without a size cap the download would fill the disk."""

    def __init__(self):
        super().__init__(b"")
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        return b"x" * 1000


def test_the_default_opener_passes_a_timeout_to_urlopen(tool, tmp_path, monkeypatch):
    """@brief Positive: urlopen gets DOWNLOAD_TIMEOUT; negative: an injected opener is called with the URL alone."""
    payload = b"weights"
    entry = _entry("a.onnx", payload)
    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append(timeout)
        return _Response(payload)
    monkeypatch.setattr(tool.urllib.request, "urlopen", fake_urlopen)
    tool.fetch(entry, tmp_path, log=lambda line: None)
    assert seen == [tool.DOWNLOAD_TIMEOUT] and 0 < tool.DOWNLOAD_TIMEOUT <= 300
    (tmp_path / "yolo" / "a.onnx").unlink()
    tool.fetch(entry, tmp_path, opener=lambda url: _Response(payload), log=lambda line: None)
    assert (tmp_path / "yolo" / "a.onnx").read_bytes() == payload


def test_a_download_longer_than_the_manifest_size_is_stopped_early(tool, tmp_path):
    """@brief Positive: an endless stream is refused as soon as it passes `size`, and no .part stays."""
    entry = _entry("a.onnx", b"y" * 2500)
    response = _EndlessResponse()
    with pytest.raises(ValueError, match="exceeds"):
        tool.fetch(entry, tmp_path, opener=lambda url: response, log=lambda line: None)
    assert response.reads == 3  # 3000 bytes > 2500: stopped at the first chunk past the size
    assert not list((tmp_path / "yolo").iterdir())


def test_a_truncated_download_is_refused_by_its_size(tool, tmp_path):
    """@brief Negative counterpart: a shorter stream is refused as truncated, not taken for the file."""
    entry = _entry("a.onnx", b"complete file")
    with pytest.raises(ValueError, match="truncated"):
        tool.fetch(entry, tmp_path, opener=lambda url: _Response(b"complete"), log=lambda line: None)
    assert not list((tmp_path / "yolo").iterdir())


def test_ctrl_c_during_the_download_leaves_no_part_file(tool, tmp_path):
    """@brief Positive: KeyboardInterrupt (not an Exception) still removes the .part and propagates."""
    entry = _entry("a.onnx", b"x" * 10)

    class _Interrupted(_Response):
        def read(self, size=-1):
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        tool.fetch(entry, tmp_path, opener=lambda url: _Interrupted(b""), log=lambda line: None)
    assert not list((tmp_path / "yolo").iterdir())


def test_a_destination_that_cannot_be_replaced_leaves_no_orphan_part(tool, tmp_path, monkeypatch):
    """@brief Positive: when the stale file cannot be removed (open elsewhere), the verified .part goes too.

    Negative: the stale destination itself is left as it was and the error propagates.
    """
    payload = b"fresh"
    entry = _entry("a.onnx", payload)
    (tmp_path / "yolo").mkdir()
    stale = tmp_path / "yolo" / "a.onnx"
    stale.write_bytes(b"stale")
    original_unlink = Path.unlink

    def locked_unlink(self, missing_ok=False):
        if self == stale:
            raise PermissionError("in use by another process")
        return original_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", locked_unlink)
    with pytest.raises(PermissionError):
        tool.fetch(entry, tmp_path, opener=lambda url: _Response(payload), log=lambda line: None)
    assert sorted(p.name for p in (tmp_path / "yolo").iterdir()) == ["a.onnx"]
    assert stale.read_bytes() == b"stale"


def test_a_network_failure_exits_1_so_the_workflow_may_retry(tool, tmp_path, monkeypatch, capsys):
    """@brief Positive: an OSError gives exit 1 (retryable); the wrong-digest case above gives 2."""
    entry = _entry("a.onnx", b"a", "https://x/a")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [entry]}), encoding="utf-8")

    def offline(url, timeout=None):
        raise OSError("network unreachable")
    monkeypatch.setattr(tool.urllib.request, "urlopen", offline)
    assert tool.main(["--manifest", str(manifest), "--target", str(tmp_path / "models")]) == 1
    assert "not fetched" in capsys.readouterr().err


# -- review 2026-10-29, K1: the build ships exactly the verified manifest ------


def _placed(tmp_path, files):
    """Write {(dir, name): bytes} under tmp_path/models; return the models folder."""
    for (folder, name), data in files.items():
        (tmp_path / "models" / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / "models" / folder / name).write_bytes(data)
    return tmp_path / "models"


def test_verified_weights_returns_exactly_the_manifest_files(tool, tmp_path):
    """@brief Positive: the manifest's files, verified, in manifest order; an unknown .onnx and a .pt are not returned."""
    detector, network = _entry("a.onnx", b"aaa"), _entry("s.onnx", b"ss")
    detector["dir"], network["dir"] = "yolo", "smd"
    models = _placed(tmp_path, {("yolo", "a.onnx"): b"aaa", ("smd", "s.onnx"): b"ss",
                                ("yolo", "extra.onnx"): b"e", ("yolo", "a.pt"): b"p"})
    assert tool.verified_weights([detector, network], models) == [
        ("yolo", models / "yolo" / "a.onnx"), ("smd", models / "smd" / "s.onnx")]


@pytest.mark.parametrize("present, reason", [
    ({}, "missing"),
    ({("yolo", "a.onnx"): b"aa"}, "bytes"),
    ({("yolo", "a.onnx"): b"aab"}, "SHA-256 differs"),
])
def test_verified_weights_refuses_a_missing_or_different_file(tool, tmp_path, present, reason):
    """@brief Negative: missing, wrong size, same size with another digest: each refused, naming the file."""
    entry = _entry("a.onnx", b"aaa")
    entry["dir"] = "yolo"
    models = _placed(tmp_path, present)
    with pytest.raises(ValueError, match=f"yolo/a.onnx: .*{reason}"):
        tool.verified_weights([entry], models)


def test_check_mode_verifies_without_downloading(tool, tmp_path, monkeypatch):
    """@brief Positive: --check passes on verified files and opens no URL; negative: a missing file exits 2."""
    entry = _entry("a.onnx", b"aaa", "https://x/a")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [entry]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(tool.urllib.request, "urlopen", _opener({}, calls))
    arguments = ["--manifest", str(manifest), "--target", str(tmp_path / "models"), "--check"]
    assert tool.main(arguments) == tool.EXIT_PERMANENT
    _placed(tmp_path, {("yolo", "a.onnx"): b"aaa"})
    assert tool.main(arguments) == 0
    assert calls == []
