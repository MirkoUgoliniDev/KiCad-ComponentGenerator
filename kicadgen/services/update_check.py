"""@brief On-demand check for a newer KiCadGen release on GitHub.

The check reads the public GitHub API endpoint of the latest published
release and compares its tag with `kicadgen.__version__`. It is meant to run
ONLY when the user asks for it from a menu: whether the program should also
check by itself at startup is a decision the user has not taken yet, so
nothing here runs on import or on a timer.

@note No new dependency: `urllib.request` from the standard library. The
    request carries no user data (no identifiers, no paths): a fixed
    User-Agent naming the program and its version, as the GitHub API asks.
@note The function never raises: every failure becomes an
    `UpdateCheckResult` with `error_kind` set, so a menu handler can show a
    message instead of losing the exception in a Qt slot.
@note The call blocks for at most about `timeout` seconds per network phase;
    the GUI should run it off the main thread.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import socket
import urllib.error
import urllib.request

from .. import __version__

#: GitHub repository that publishes the releases (owner/name).
REPOSITORY = "MirkoUgoliniDev/KiCad-ComponentGenerator"
#: API endpoint of the latest release: drafts and prereleases are excluded
#: by GitHub itself.
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
#: Human page listing every release; also the fallback link of every result.
RELEASES_PAGE_URL = f"https://github.com/{REPOSITORY}/releases"
#: Default timeout in seconds: short, the user is waiting on a menu command.
DEFAULT_TIMEOUT_S = 5.0
#: Upper bound of the response read, in bytes: a release document is a few
#: kilobytes; anything far larger is not the expected answer.
MAX_RESPONSE_BYTES = 1_000_000

#: Values of `UpdateCheckResult.error_kind`, stable for the GUI to translate.
ERROR_TIMEOUT = "timeout"
ERROR_NETWORK = "network"
ERROR_HTTP = "http"
ERROR_NO_RELEASE = "no_release"
ERROR_INVALID_RESPONSE = "invalid_response"

_VERSION_RE = re.compile(
    r"^[vV]?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class UpdateCheckResult:
    """@brief Outcome of one update check.

    @note `latest_version` is normalized (no leading "v"); `tag` is the tag as
        published. `release_url` is always safe to open: the release page
        from the answer only when it belongs to this repository, otherwise the
        list of releases.
    @note `update_available` is True only when the published version is
        strictly newer than `current_version` AND is not a prerelease: a
        stable-channel check never proposes a release candidate.
    @note `error` is a short technical detail in English (for the log);
        the GUI builds the user message from `error_kind`.
    """

    current_version: str
    latest_version: str | None = None
    tag: str | None = None
    release_url: str = RELEASES_PAGE_URL
    update_available: bool = False
    prerelease: bool = False
    error_kind: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """@brief Tell whether the check produced a comparable answer.

        @return True when no error occurred.
        """
        return self.error_kind is None


def parse_version(text: object) -> tuple[tuple[int, int, int], tuple] | None:
    """@brief Parse a semantic version such as "1.2.0", "v1.2" or "v2.0.0-rc.1".

    @param text Version or tag text.
    @return (numbers, prerelease) where numbers is (major, minor, patch) and
        prerelease is a tuple of identifiers (empty for a final release), or
        None when the text is not a version.
    @note Build metadata after "+" is accepted and ignored, as SemVer says.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    numbers = (int(match["major"]), int(match["minor"]), int(match["patch"] or 0))
    pre = match["pre"]
    identifiers: tuple = ()
    if pre:
        parts = pre.split(".")
        if any(part == "" for part in parts):
            return None
        identifiers = tuple(int(part) if part.isdigit() else part for part in parts)
    return numbers, identifiers


def _precedence_key(parsed: tuple[tuple[int, int, int], tuple]) -> tuple:
    """Sort key implementing SemVer 2.0 precedence (final > any prerelease)."""
    numbers, identifiers = parsed
    if not identifiers:
        return numbers, 1, ()
    # Numeric identifiers sort before alphanumeric ones, then by value.
    return numbers, 0, tuple((0, part, "") if isinstance(part, int) else (1, 0, part)
                             for part in identifiers)


def compare_versions(left: str, right: str) -> int | None:
    """@brief Compare two version strings with SemVer precedence.

    @param left First version or tag.
    @param right Second version or tag.
    @return -1, 0 or 1 like a classic comparator; None when either side is
        not a valid version.
    """
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return None
    ka, kb = _precedence_key(a), _precedence_key(b)
    return (ka > kb) - (ka < kb)


def _safe_release_url(value: object) -> str:
    """Keep the release page only when it points inside this repository."""
    prefix = f"https://github.com/{REPOSITORY}/"
    if isinstance(value, str) and value.startswith(prefix) and not any(
            character.isspace() for character in value):
        return value
    return RELEASES_PAGE_URL


def check_latest_release(timeout: float = DEFAULT_TIMEOUT_S, *,
                         current_version: str | None = None) -> UpdateCheckResult:
    """@brief Ask GitHub for the latest release and compare it with this program.

    @param timeout Seconds allowed to the connection and to each read.
    @param current_version Version to compare with; defaults to
        `kicadgen.__version__` (tests pass their own).
    @return An `UpdateCheckResult`; on failure `error_kind` is one of
        "timeout", "network", "http", "no_release" (HTTP 404: nothing
        published yet) or "invalid_response".
    @note Never raises: this is a convenience check, and a broken network or
        an unexpected answer must not become an unhandled exception.
    """
    current = str(current_version or __version__)
    try:
        request = urllib.request.Request(LATEST_RELEASE_API_URL, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"KiCadGen/{current} (update check)",
        })
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return UpdateCheckResult(current, error_kind=ERROR_INVALID_RESPONSE,
                                     error="response too large")
        data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        kind = ERROR_NO_RELEASE if exc.code == 404 else ERROR_HTTP
        return UpdateCheckResult(current, error_kind=kind, error=f"HTTP {exc.code}")
    except (socket.timeout, TimeoutError) as exc:
        return UpdateCheckResult(current, error_kind=ERROR_TIMEOUT, error=str(exc) or "timeout")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        kind = ERROR_TIMEOUT if isinstance(reason, (socket.timeout, TimeoutError)) else ERROR_NETWORK
        return UpdateCheckResult(current, error_kind=kind, error=str(reason))
    except (ValueError, UnicodeDecodeError) as exc:
        return UpdateCheckResult(current, error_kind=ERROR_INVALID_RESPONSE,
                                 error=f"not JSON: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - the contract is «never raises»
        return UpdateCheckResult(current, error_kind=ERROR_NETWORK,
                                 error=f"{type(exc).__name__}: {exc}")
    return _result_from_release(current, data)


def _result_from_release(current: str, data: object) -> UpdateCheckResult:
    """Validate the release document and build the comparison result."""
    if not isinstance(data, dict):
        return UpdateCheckResult(current, error_kind=ERROR_INVALID_RESPONSE,
                                 error="release document is not an object")
    tag = data.get("tag_name")
    parsed = parse_version(tag)
    if parsed is None:
        return UpdateCheckResult(current, tag=tag if isinstance(tag, str) else None,
                                 error_kind=ERROR_INVALID_RESPONSE,
                                 error="tag_name is not a version")
    numbers, identifiers = parsed
    latest = ".".join(str(n) for n in numbers)
    if identifiers:
        latest += "-" + ".".join(str(part) for part in identifiers)
    prerelease = bool(identifiers) or data.get("prerelease") is True or data.get("draft") is True
    comparison = compare_versions(latest, current)
    if comparison is None:
        return UpdateCheckResult(current, latest_version=latest, tag=tag,
                                 release_url=_safe_release_url(data.get("html_url")),
                                 prerelease=prerelease, error_kind=ERROR_INVALID_RESPONSE,
                                 error=f"current version {current!r} is not a version")
    return UpdateCheckResult(
        current, latest_version=latest, tag=tag,
        release_url=_safe_release_url(data.get("html_url")),
        update_available=comparison > 0 and not prerelease,
        prerelease=prerelease)
