"""@brief Build the «Segnala un problema» link: a pre-filled GitHub issue.

The link opens https://github.com/MirkoUgoliniDev/KiCad-ComponentGenerator/issues/new
with a title and a body that already carry the program version, the
operating system, Python/Qt and whether FreeCAD and KiCad were found, plus a
checklist asking the user to attach the log files from the folder that the
program opens for them.

@note Nothing is sent: the function only builds a URL. The user reads the
    page, completes it and decides whether to submit it.
@note Privacy rule: the URL never contains the user name, the machine name
    or a home path. Once submitted, the issue is PUBLIC, and before that the
    URL is kept in the browser history; Windows account names are often real
    names. So the body names the log FILES and says where they are only in
    generic terms («the folder KiCadGen opened»), versions of FreeCAD/KiCad
    are reduced to digits taken from the installation folder name, and the
    whole text goes through `kicadgen.crash_report.scrub` as a last guard.
    The log files themselves are attached by hand, after the user has looked
    at them (the checklist says so).
@note The body headings are English by default (the issue tracker is the
    project's technical channel); the GUI may pass translated texts through
    `texts` from the catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
import platform
import re
import struct
import sys
from typing import Mapping, Optional
from urllib.parse import quote

from .. import __version__
from ..crash_report import scrub

#: Page that opens a new issue on the project repository.
ISSUES_NEW_URL = "https://github.com/MirkoUgoliniDev/KiCad-ComponentGenerator/issues/new"
#: Upper bound of the whole URL. Browsers and GitHub accept more, but very
#: long URLs are truncated by some mail clients and proxies; 6000 leaves room.
MAX_URL_LENGTH = 6000
#: Maximum length of the title, in characters.
MAX_TITLE_LENGTH = 200

#: Default texts of the body. Keys are stable: the GUI may override any of
#: them with a translation.
DEFAULT_TEXTS: dict[str, str] = {
    "default_title": "Problem report",
    "description": "Description",
    "description_hint": "What happened, and what did you expect?",
    "steps": "Steps to reproduce",
    "environment": "Environment",
    "attachments": "Attachments",
    "attachments_hint": ("KiCadGen has opened its log folder. Drag the files you "
                         "want to share onto this page, after checking them: they "
                         "may contain names of your folders and projects."),
    "attach_analysis_log": "`analisi-ai.log` from the project folder",
    "attach_freecad_log": "`freecad.log` from the project folder (3D problems)",
    "attach_crash_report": "the latest file in `crash-reports` (if the program crashed)",
    "attach_screenshot": "a screenshot of the problem, if useful",
    "found": "found",
    "not_found": "not found",
    "unknown": "unknown",
    "truncated": "(text shortened to fit the link)",
}

_VERSION_TOKEN = re.compile(r"(\d+(?:\.\d+){0,3})")


@dataclass(frozen=True)
class IssueEnvironment:
    """@brief The facts written in the «Environment» section.

    @note Every field is already free of personal data: versions and
        found/not-found flags only, never paths.
    """

    app_version: str
    os: str
    python: str
    qt: str
    freecad_found: Optional[bool]
    freecad_version: Optional[str]
    kicad_found: Optional[bool]
    kicad_version: Optional[str]
    frozen: bool


def _tool_version(path: Optional[str]) -> Optional[str]:
    """Version digits from an installation folder name, e.g. «KiCad\\9.0\\bin» -> "9.0"."""
    if not path:
        return None
    parts = re.split(r"[\\/]+", str(path))
    # Only folder names after the product folder are inspected: a version-like
    # token elsewhere in the path could come from a user-chosen folder.
    for index, part in enumerate(parts):
        if re.search(r"(?i)kicad|freecad", part):
            for candidate in [part] + parts[index + 1:index + 2]:
                match = _VERSION_TOKEN.search(candidate)
                if match:
                    return match.group(1)
    return None


def _describe_tool(found: Optional[bool], version: Optional[str], texts: Mapping[str, str]) -> str:
    """Render one tool line: unknown, not found, or found with its version."""
    if found is None:
        return texts["unknown"]
    if not found:
        return texts["not_found"]
    return f"{texts['found']} ({version})" if version else texts["found"]


def collect_environment(*, detect_applications: bool = True,
                        freecad_path: Optional[str] = None,
                        kicad_path: Optional[str] = None) -> IssueEnvironment:
    """@brief Gather the non-personal facts of the running installation.

    @param detect_applications When True and no path is given, run the
        existing `system_dependencies.detect_external_applications` (file
        lookups only, no process is started). When False the tools are
        reported as unknown unless a path is given.
    @param freecad_path Known freecadcmd path, e.g. the one the window
        already detected; only its version digits are kept.
    @param kicad_path Known kicad-cli path; same treatment.
    @return An `IssueEnvironment`.
    @note Never raises: a failed detection becomes «unknown».
    """
    freecad_found: Optional[bool] = True if freecad_path else None
    kicad_found: Optional[bool] = True if kicad_path else None
    if detect_applications and not (freecad_path and kicad_path):
        try:
            from .system_dependencies import detect_external_applications
            detected = detect_external_applications()
            if not freecad_path:
                freecad_path = detected.freecadcmd
                freecad_found = bool(detected.freecadcmd)
            if not kicad_path:
                kicad_path = detected.kicad_cli
                kicad_found = bool(detected.kicad_cli)
        except Exception:  # noqa: BLE001 - a report without tools beats no report
            pass
    core = sys.modules.get("PyQt6.QtCore")
    qt = (f"Qt {getattr(core, 'QT_VERSION_STR', '?')}, PyQt {getattr(core, 'PYQT_VERSION_STR', '?')}"
          if core is not None else "")
    return IssueEnvironment(
        app_version=str(__version__),
        os=platform.platform(),
        python=f"{platform.python_version()} ({8 * struct.calcsize('P')}-bit)",
        qt=qt,
        freecad_found=freecad_found,
        freecad_version=_tool_version(freecad_path) if freecad_found else None,
        kicad_found=kicad_found,
        kicad_version=_tool_version(kicad_path) if kicad_found else None,
        frozen=bool(getattr(sys, "frozen", False)),
    )


def build_issue_body(environment: IssueEnvironment,
                     texts: Optional[Mapping[str, str]] = None) -> str:
    """@brief Compose the Markdown body of the issue.

    @param environment Facts from `collect_environment`.
    @param texts Optional overrides of `DEFAULT_TEXTS` (e.g. translations).
    @return The scrubbed Markdown text.
    """
    t = dict(DEFAULT_TEXTS)
    t.update({key: value for key, value in (texts or {}).items() if isinstance(value, str)})
    build = "installed" if environment.frozen else "from sources"
    lines = [
        f"### {t['description']}",
        f"<!-- {t['description_hint']} -->",
        "",
        "",
        f"### {t['steps']}",
        "1. ",
        "2. ",
        "",
        f"### {t['environment']}",
        f"- KiCadGen: {environment.app_version} ({build})",
        f"- OS: {environment.os}",
        f"- Python: {environment.python}",
        f"- Qt: {environment.qt or t['unknown']}",
        f"- FreeCAD: {_describe_tool(environment.freecad_found, environment.freecad_version, t)}",
        f"- KiCad: {_describe_tool(environment.kicad_found, environment.kicad_version, t)}",
        "",
        f"### {t['attachments']}",
        t["attachments_hint"],
        "",
        f"- [ ] {t['attach_analysis_log']}",
        f"- [ ] {t['attach_freecad_log']}",
        f"- [ ] {t['attach_crash_report']}",
        f"- [ ] {t['attach_screenshot']}",
    ]
    return scrub("\n".join(lines) + "\n")


def _encode(title: str, body: str) -> str:
    return f"{ISSUES_NEW_URL}?title={quote(title, safe='')}&body={quote(body, safe='')}"


def build_issue_url(title: str = "", *, environment: Optional[IssueEnvironment] = None,
                    texts: Optional[Mapping[str, str]] = None,
                    max_length: int = MAX_URL_LENGTH) -> str:
    """@brief Build the «new issue» URL with title and pre-filled body.

    @param title Issue title; defaults to the «default_title» text. Control
        characters are removed and the length is capped at MAX_TITLE_LENGTH.
    @param environment Facts to write; collected with defaults when None.
    @param texts Optional overrides of `DEFAULT_TEXTS`.
    @param max_length Upper bound of the whole URL, in characters.
    @return An https URL no longer than `max_length`.
    @note When the body does not fit, it is cut at a line boundary and the
        «truncated» note is appended; with a limit too small even for that,
        the body is dropped and only the title is kept.
    """
    t = dict(DEFAULT_TEXTS)
    t.update({key: value for key, value in (texts or {}).items() if isinstance(value, str)})
    clean_title = re.sub(r"[\x00-\x1f\x7f]+", " ", scrub(title or t["default_title"])).strip()
    clean_title = (clean_title or t["default_title"])[:MAX_TITLE_LENGTH]
    body = build_issue_body(environment or collect_environment(), t)
    url = _encode(clean_title, body)
    if len(url) <= max_length:
        return url
    lines = body.splitlines()
    note = t["truncated"]
    while lines:
        lines.pop()
        candidate = _encode(clean_title, "\n".join(lines + ["", note]) + "\n")
        if len(candidate) <= max_length:
            return candidate
    minimal = f"{ISSUES_NEW_URL}?title={quote(clean_title, safe='')}"
    while len(minimal) > max_length and clean_title:
        clean_title = clean_title[:-1]
        minimal = f"{ISSUES_NEW_URL}?title={quote(clean_title, safe='')}"
    return minimal if len(minimal) <= max_length else ISSUES_NEW_URL
