"""@brief On-demand update check against the latest GitHub release (no real network)."""
import importlib
import io
import json
import socket
import urllib.error
import urllib.request

import pytest

from kicadgen.services import update_check as uc


class _Response(io.BytesIO):
    """Minimal context-manager response, like the object urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _serve(monkeypatch, payload=None, *, raw=None, error=None, calls=None):
    """Replace urllib.request.urlopen with a fake that serves one answer."""
    def fake_urlopen(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        return _Response(body)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _release(tag, **extra):
    data = {"tag_name": tag, "draft": False, "prerelease": False,
            "html_url": f"https://github.com/{uc.REPOSITORY}/releases/tag/{tag}"}
    data.update(extra)
    return data


def test_newer_release_is_offered_with_its_page(monkeypatch):
    """@brief Positive: v1.2.0 against 1.0.0 is an update, with the release page."""
    calls = []
    _serve(monkeypatch, _release("v1.2.0"), calls=calls)
    result = uc.check_latest_release(timeout=3, current_version="1.0.0")
    assert result.ok and result.update_available
    assert result.latest_version == "1.2.0" and result.tag == "v1.2.0"
    assert result.release_url.endswith("/releases/tag/v1.2.0")
    request, timeout = calls[0]
    assert request.full_url == uc.LATEST_RELEASE_API_URL and timeout == 3
    assert request.get_header("User-agent").startswith("KiCadGen/1.0.0")


@pytest.mark.parametrize("tag", ["v1.0.0", "1.0.0", "v1.0", "V1.0.0+build.7"])
def test_same_version_is_not_an_update(monkeypatch, tag):
    """@brief Negative: equal versions, however the tag is spelled, are not updates."""
    _serve(monkeypatch, _release(tag))
    result = uc.check_latest_release(current_version="1.0.0")
    assert result.ok and not result.update_available


def test_older_release_is_not_an_update(monkeypatch):
    """@brief Negative: a development build newer than the release is not downgraded."""
    _serve(monkeypatch, _release("v0.9.5"))
    result = uc.check_latest_release(current_version="1.0.0")
    assert result.ok and not result.update_available and result.latest_version == "0.9.5"


def test_numeric_not_lexicographic_comparison(monkeypatch):
    """@brief 1.10.0 is newer than 1.9.0 (a string comparison would say the opposite)."""
    _serve(monkeypatch, _release("v1.10.0"))
    assert uc.check_latest_release(current_version="1.9.0").update_available


@pytest.mark.parametrize("tag,flag,current,expected", [
    ("v1.1.0-rc.1", False, "1.0.0", False),  # newer, but a prerelease tag
    ("v1.1.0", True, "1.0.0", False),         # newer, but flagged prerelease by GitHub
    ("v1.1.0-rc.1", False, "1.1.0", False),   # older than the final release
])
def test_prereleases_are_never_offered(monkeypatch, tag, flag, current, expected):
    """@brief Prerelease tags and flags stay out of the stable-channel answer."""
    _serve(monkeypatch, _release(tag, prerelease=flag))
    result = uc.check_latest_release(current_version=current)
    assert result.ok and result.prerelease and result.update_available is expected


def test_semver_precedence_of_prerelease_identifiers():
    """@brief SemVer 2.0 ordering, positive and negative cases."""
    assert uc.compare_versions("1.0.0-alpha", "1.0.0") == -1
    assert uc.compare_versions("1.0.0-alpha.1", "1.0.0-alpha.beta") == -1
    assert uc.compare_versions("1.0.0-rc.10", "1.0.0-rc.2") == 1
    assert uc.compare_versions("v2.0.0", "1.99.99") == 1
    assert uc.compare_versions("1.0.0", "1.0") == 0
    assert uc.compare_versions("latest", "1.0.0") is None
    assert uc.parse_version("1.0.0-") is None and uc.parse_version("1.0.0-a..b") is None


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b"[1, 2]",
                                 json.dumps({"tag_name": "nightly"}).encode(),
                                 json.dumps({"name": "no tag"}).encode()])
def test_malformed_answers_become_invalid_response(monkeypatch, raw):
    """@brief Broken JSON, a non-object or a non-version tag never raise."""
    _serve(monkeypatch, raw=raw)
    result = uc.check_latest_release(current_version="1.0.0")
    assert not result.ok and result.error_kind == uc.ERROR_INVALID_RESPONSE
    assert not result.update_available


def test_oversized_answer_is_rejected(monkeypatch):
    """@brief A response larger than the bound is not parsed."""
    _serve(monkeypatch, raw=b" " * (uc.MAX_RESPONSE_BYTES + 10))
    result = uc.check_latest_release(current_version="1.0.0")
    assert result.error_kind == uc.ERROR_INVALID_RESPONSE


@pytest.mark.parametrize("error,kind", [
    (socket.timeout("timed out"), uc.ERROR_TIMEOUT),
    (TimeoutError("read timed out"), uc.ERROR_TIMEOUT),
    (urllib.error.URLError(socket.timeout("timed out")), uc.ERROR_TIMEOUT),
    (urllib.error.URLError("getaddrinfo failed"), uc.ERROR_NETWORK),
    (urllib.error.HTTPError(uc.LATEST_RELEASE_API_URL, 404, "Not Found", {}, None),
     uc.ERROR_NO_RELEASE),
    (urllib.error.HTTPError(uc.LATEST_RELEASE_API_URL, 403, "rate limited", {}, None),
     uc.ERROR_HTTP),
    (RuntimeError("unexpected"), uc.ERROR_NETWORK),
])
def test_failures_never_raise(monkeypatch, error, kind):
    """@brief Timeouts, network and HTTP errors are reported, never raised."""
    _serve(monkeypatch, error=error)
    result = uc.check_latest_release(timeout=0.1, current_version="1.0.0")
    assert result.error_kind == kind and result.error
    assert not result.update_available and result.release_url == uc.RELEASES_PAGE_URL


def test_foreign_release_link_is_replaced(monkeypatch):
    """@brief Negative: a release page outside the repository is never offered."""
    _serve(monkeypatch, _release("v9.0.0", html_url="https://evil.example/download"))
    result = uc.check_latest_release(current_version="1.0.0")
    assert result.update_available and result.release_url == uc.RELEASES_PAGE_URL


def test_default_current_version_is_the_package_version(monkeypatch):
    """@brief Without an explicit version the package version is compared."""
    import kicadgen
    _serve(monkeypatch, _release("v" + kicadgen.__version__))
    result = uc.check_latest_release()
    assert result.current_version == kicadgen.__version__ and not result.update_available


def test_import_makes_no_network_call(monkeypatch):
    """@brief Importing (or reloading) the module never touches the network."""
    def forbidden(*args, **kwargs):
        raise AssertionError("network used at import time")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    importlib.reload(uc)
