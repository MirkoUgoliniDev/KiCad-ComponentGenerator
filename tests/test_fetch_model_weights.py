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
    def open_url(url):
        calls.append(url)
        return _Response(payloads[url])
    return open_url


def _entry(name, payload, url="https://example.invalid/w.onnx"):
    return {"file": name, "sha256": hashlib.sha256(payload).hexdigest(), "url": url}


def test_the_manifest_digests_are_the_ones_the_program_and_the_notices_record(tool):
    """@brief Every manifest SHA-256 is a notices entry (the detectors' a KNOWN_MODELS key too); URLs point at the «models» release."""
    entries = tool.load_manifest(ROOT / "packaging" / "model-weights.json")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert len(entries) == 5
    assert {entry["dir"] for entry in entries} == {"yolo", "smd"}
    for entry in entries:
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
    opener = _opener({entry["url"]: b"something else"}, [])
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


@pytest.mark.parametrize("entry", [
    {"file": "../escape.onnx", "sha256": "0" * 64, "url": "https://x/y"},
    {"file": "a.onnx", "dir": "../escape", "sha256": "0" * 64, "url": "https://x/y"},
    {"file": "a.onnx", "sha256": "abc", "url": "https://x/y"},
    {"file": "a.onnx", "sha256": "0" * 64, "url": "http://x/y"},
    {"file": "a.onnx", "sha256": "0" * 64},
])
def test_a_malformed_manifest_is_refused(tool, tmp_path, entry):
    """@brief Negative: paths, short digests, plain http and missing fields never reach a download."""
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [entry]}), encoding="utf-8")
    with pytest.raises(ValueError):
        tool.load_manifest(manifest)


def test_main_stops_at_the_first_failure(tool, tmp_path, monkeypatch, capsys):
    """@brief Negative: with a wrong asset the exit status is 1 and the later file is not fetched."""
    good, bad = _entry("good.onnx", b"g", "https://x/good"), _entry("bad.onnx", b"b", "https://x/bad")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"weights": [bad, good]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(tool.urllib.request, "urlopen", _opener({"https://x/bad": b"other", "https://x/good": b"g"}, calls))
    assert tool.main(["--manifest", str(manifest), "--target", str(tmp_path / "models")]) == 1
    assert calls == ["https://x/bad"] and "differs" in capsys.readouterr().err
